"""Speaking-rate contracts, without hardware or production API calls."""

import json
import os
import queue
import subprocess
import sys
import threading
import wave
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from hal.drivers.voice.tts.elevenlabs import ElevenLabsTTSBackend
from hal.drivers.voice.tts.elevenlabs_ws import ElevenLabsWSTTSBackend
from hal.drivers.voice.tts.service import TTSService
from hal.models import SpeakRequest, TTSConfigRequest, VoiceStartRequest
from hal.routes import voice


@pytest.mark.parametrize("speed", [0.7, 1.0, 1.2])
def test_http_overrides_stored_voice_speed_even_at_normal(speed):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=b"\x00\x00")

    backend = object.__new__(ElevenLabsTTSBackend)
    backend._api_key = "test-only"
    backend._base_url = "https://proxy.example/v1/elevenlabs"
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        backend._client = client
        assert b"".join(backend.stream_pcm("Hello", "Rachel", "eleven_v3", speed)) == b"\x00\x00"
    assert json.loads(requests[0].content)["voice_settings"]["speed"] == speed
    assert requests[0].url.path.endswith("/stream")


@pytest.mark.parametrize("speed", [0.7, 1.0, 1.2])
def test_websocket_overrides_stored_voice_speed_even_at_normal(speed):
    messages = []
    socket = SimpleNamespace(send=lambda text: messages.append(json.loads(text)), close=lambda: None)

    class Connection:
        send = staticmethod(socket.send)
        close = staticmethod(socket.close)

        def __iter__(self):
            return iter([json.dumps({"audio": "AAA=", "isFinal": True})])

    backend = object.__new__(ElevenLabsWSTTSBackend)
    backend._api_key = "test-only"
    backend._url_tmpl = "wss://proxy.example/{voice_id}?model_id={model}"
    backend._connect = lambda *args, **kwargs: Connection()
    assert b"".join(backend.stream_pcm("Hello", "Rachel", "", speed)) == b"\x00\x00"
    assert messages[0]["voice_settings"]["speed"] == speed


@pytest.mark.parametrize("bad", [0, 0.69, 1.21, float("nan"), float("inf"), "bad"])
@pytest.mark.parametrize("model,field,required", [
    (SpeakRequest, "speed", {"text": "Hello"}),
    (TTSConfigRequest, "speed", {}),
    (VoiceStartRequest, "tts_speed", {"llm_api_key": "test", "llm_base_url": "https://proxy.example"}),
])
def test_invalid_speed_is_rejected_before_touching_runtime(model, field, required, bad):
    with pytest.raises(ValidationError):
        model(**required, **{field: bad})


class FakeTTS:
    available = True
    speaking = False

    def __init__(self, voice="Rachel", speed=1.1, instructions=None, provider="elevenlabs", **kwargs):
        self._voice = voice
        self._speed = speed
        self._instructions = instructions
        self._provider = provider
        self._backend = SimpleNamespace(_api_key="test", _base_url="https://proxy.example/v1/elevenlabs")
        self.spoken_speeds = []

    def speak(self, text, speed=None, **kwargs):
        self.spoken_speeds.append(self._speed if speed is None else speed)
        return True


@pytest.fixture
def runtime(monkeypatch):
    service = FakeTTS()
    monkeypatch.setattr(voice.state, "tts_service", service)
    monkeypatch.setattr(voice.state, "voice_service", SimpleNamespace(available=True))
    monkeypatch.setattr(voice.state, "music_service", None)
    monkeypatch.setattr(voice.state, "simulation_audio", False)
    monkeypatch.setattr(voice, "TTSService", FakeTTS)
    monkeypatch.setattr(voice, "threading", SimpleNamespace(Thread=lambda **kwargs: SimpleNamespace(start=lambda: None)))
    return service


def test_live_partial_update_preserves_speed_and_explicit_normal_resets_it(runtime):
    voice.update_tts_config(TTSConfigRequest(voice="Sarah"))
    assert runtime._speed == 1.1
    voice.update_tts_config(TTSConfigRequest(speed=1.0))
    assert runtime._speed == 1.0


def test_preview_uses_unsaved_speed_without_changing_future_speech(runtime):
    voice.speak_text(SpeakRequest(text="Preview", speed=1.2))
    voice.speak_text(SpeakRequest(text="Another preview"))
    assert runtime.spoken_speeds == [1.2, 1.1]
    assert runtime._speed == 1.1


def test_rejected_preview_does_not_change_live_speed(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "speak", lambda *args, **kwargs: False)
    with pytest.raises(HTTPException) as error:
        voice.speak_text(SpeakRequest(text="Preview", speed=1.2))
    assert error.value.status_code == 409
    assert runtime._speed == 1.1


@pytest.mark.parametrize("speed,expected", [(None, 1.1), (1.0, 1.0), (1.2, 1.2)])
def test_voice_restart_uses_requested_speed_or_preserves_live_speed(runtime, speed, expected):
    # A different voice forces reconstruction, as happens on a provider change.
    voice.start_voice(VoiceStartRequest(
        llm_api_key="test", llm_base_url="https://proxy.example",
        tts_voice="Sarah", tts_provider="elevenlabs", tts_speed=speed,
    ))
    assert voice.state.tts_service is not runtime
    assert voice.state.tts_service._speed == expected


def test_changing_speed_invalidates_audio_cache():
    service = object.__new__(TTSService)
    service._provider = "elevenlabs"
    service._voice = "Rachel"
    service._model = "eleven_v3"
    service._speed = 1.0
    normal = service._tts_cache_key("Hello")
    service._speed = 1.2
    assert service._tts_cache_key("Hello") != normal


def test_synthesis_workers_keep_preview_speed_separate_from_saved_speed(tmp_path):
    rates = []

    def render(**kwargs):
        rates.append(kwargs["speed"])
        return iter([b"\x00\x00"])

    service = object.__new__(TTSService)
    service._provider = "elevenlabs"
    service._voice = "Rachel"
    service._model = "eleven_v3"
    service._speed = 1.1
    service._instructions = None
    service._np = np
    service._max_retries = 0
    service._stop_event = threading.Event()
    service._backend = SimpleNamespace(sample_rate=24000, volume_boost=1, stream_pcm=render)

    ordinary_key = service._tts_cache_key("Hello")
    preview_key = service._tts_cache_key("Hello", speed=1.2)
    assert ordinary_key != preview_key
    service._head_producer("Hello", 24000, queue.Queue(), (1, 3), speed=1.2)
    service._tail_producer(["Second", "Third"], 24000, queue.Queue(), speed=1.2)
    path = tmp_path / (preview_key + ".wav")
    service._render_and_save_wav("Hello", path, speed=1.2)
    with wave.open(str(path), "rb") as wav:
        assert wav.getnframes() == 1
    list(service._iter_tts_samples("Ordinary speech", 24000))
    assert rates == [1.2, 1.2, 1.2, 1.2, 1.1]
    assert service._speed == 1.1
    assert service._tts_cache_key("Hello") == ordinary_key


@pytest.mark.parametrize("value,expected", [("1.1", 1.1), ("bad", 1.0), ("nan", 1.0), ("inf", 1.0)])
def test_legacy_speed_env_is_preserved_or_defaults_when_invalid(value, expected):
    result = subprocess.check_output(
        [sys.executable, "-c", "from hal.config import TTS_SPEED; print(TTS_SPEED)"],
        env={**os.environ, "HAL_TTS_SPEED": value}, text=True,
    )
    assert float(result.strip()) == expected
