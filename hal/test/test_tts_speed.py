"""Saved speed reuses the existing TTS pipeline and provider settings."""

import json
from types import SimpleNamespace

import httpx
import pytest

from hal import config
from hal.drivers.voice.tts.elevenlabs import ElevenLabsTTSBackend
from hal.drivers.voice.tts.elevenlabs_ws import ElevenLabsWSTTSBackend
from hal.models import TTSConfigRequest, VoiceStartRequest
from hal.routes import voice


def test_elevenlabs_normal_speed_is_sent_explicitly():
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=b"\x00\x00")

    backend = object.__new__(ElevenLabsTTSBackend)
    backend._api_key = "test-only"
    backend._base_url = "https://proxy.example/v1/elevenlabs"
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        backend._client = client
        list(backend.stream_pcm("Hello", "Rachel", "eleven_v3", 1.0))
    assert requests[0]["voice_settings"]["speed"] == 1.0

    class Socket:
        def send(self, text):
            requests.append(json.loads(text))

        def close(self):
            pass

        def __iter__(self):
            return iter([])

    ws = object.__new__(ElevenLabsWSTTSBackend)
    ws._api_key = "test-only"
    ws._url_tmpl = "wss://proxy.example/{voice_id}?model_id={model}"
    ws._connect = lambda *args, **kwargs: Socket()
    list(ws.stream_pcm("Hello", "Rachel", "", 1.0))
    assert requests[1]["voice_settings"]["speed"] == 1.0


@pytest.mark.parametrize("saved", [None, 0, 5, "bad", True])
def test_missing_or_invalid_saved_speed_preserves_legacy_env(tmp_path, monkeypatch, saved):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "OS_CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "TTS_SPEED", 1.5)
    assert config.get_tts_speed() == 1.5
    path.write_text(json.dumps({"tts_speed": saved}))
    assert config.get_tts_speed() == 1.5


def test_voice_recreation_reads_latest_saved_speed_without_go_startup_field(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "OS_CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "TTS_SPEED", 1.3)
    monkeypatch.setattr(voice.state, "simulation_audio", False)
    monkeypatch.setattr(voice.state, "voice_service", SimpleNamespace(available=True))
    monkeypatch.setattr(voice.state, "music_service", None)
    monkeypatch.setattr(voice, "TTSService", lambda **kwargs: SimpleNamespace(_speed=kwargs["speed"]))
    for speed in [1.2, 1.5]:
        path.write_text(json.dumps({"tts_speed": speed}))
        monkeypatch.setattr(voice.state, "tts_service", None)
        voice.start_voice(VoiceStartRequest(llm_api_key="test", llm_base_url="https://proxy.example"))
        assert voice.state.tts_service._speed == speed


def test_existing_hal_config_endpoint_still_accepts_openai_speed(monkeypatch):
    service = SimpleNamespace(_speed=1.3, _voice="nova", _provider="openai",
                              _backend=SimpleNamespace(_api_key="", _base_url=""))
    monkeypatch.setattr(voice.state, "tts_service", service)
    monkeypatch.setattr(voice, "threading", SimpleNamespace(Thread=lambda **kwargs: SimpleNamespace(start=lambda: None)))
    voice.update_tts_config(TTSConfigRequest(speed=1.5))
    assert service._speed == 1.5
