"""Local v3 tempo preserves pitch, streams early, and leaves other models alone."""

import json
import shutil

import httpx
import numpy as np
import pytest

from hal.drivers.voice.tts.elevenlabs import ElevenLabsTTSBackend
from hal.drivers.voice.tts.tempo import change_tempo


@pytest.mark.parametrize("speed", [0.25, 0.8, 1.15, 2.0, 4.0])
def test_duration_and_pitch(speed):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg unavailable")
    rate = 24000
    source = (np.sin(np.arange(rate * 3) * 2 * np.pi * 440 / rate) * 12000).astype("<i2").tobytes()
    out = b"".join(change_tempo((source[i:i+4096] for i in range(0, len(source), 4096)), speed, rate))
    samples = np.frombuffer(out, dtype="<i2")
    assert len(samples) / rate == pytest.approx(3 / speed, abs=0.12)
    spectrum = np.abs(np.fft.rfft(samples))
    frequency = np.argmax(spectrum) * rate / len(samples)
    assert frequency == pytest.approx(440, abs=4)


def test_normal_speed_bypasses_process(monkeypatch):
    monkeypatch.setattr("hal.drivers.voice.tts.tempo.subprocess.Popen", lambda *a, **k: pytest.fail("process started"))
    assert list(change_tempo([b"abc", b"de"], 1.0, 24000)) == [b"abc", b"de"]


def test_streams_before_input_eof_and_closes():
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg unavailable")
    finished = []

    def source():
        for _ in range(10000):
            yield b"\x01\x00" * 2048
        finished.append(True)

    output = change_tempo(source(), 1.15, 24000)
    assert next(output)
    assert not finished
    output.close()


@pytest.mark.parametrize("model,provider_speed,local", [
    ("eleven_v3", 1.0, True), ("tts-1", 1.0, True),
    ("eleven_multilingual_v2", 1.2, False),
])
def test_only_effective_v3_gets_local_speed(monkeypatch, model, provider_speed, local):
    requests, tempos = [], []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=b"\x01\x00")

    def tempo(chunks, speed, rate):
        tempos.append((speed, rate))
        yield from chunks

    monkeypatch.setattr("hal.drivers.voice.tts.elevenlabs.change_tempo", tempo)
    backend = object.__new__(ElevenLabsTTSBackend)
    backend._api_key = "test"
    backend._base_url = "https://test.example"
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        backend._client = client
        assert b"".join(backend.stream_pcm("Hello", "Ngan", model, 1.5)) == b"\x01\x00"
    assert requests[0]["voice_settings"]["speed"] == provider_speed
    assert tempos == ([(1.5, 24000)] if local else [])
