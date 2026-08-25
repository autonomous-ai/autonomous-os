"""Tests for the OpenAI-compatible STT provider (batch /audio/transcriptions)
and the shared stt_provider selection helper."""

import io
import wave

import pytest

import hal.drivers.voice.stt.openai as openai_stt_module
from hal.drivers.voice.stt.autonomous import AutonomousSTT
from hal.drivers.voice.stt.deepgram import DeepgramSTT
from hal.drivers.voice.stt.openai import OpenAISTT, OpenAISTTSession, _wrap_wav
from hal.drivers.voice.stt.select import select_stt_provider


def test_wrap_wav_header_is_correct():
    pcm = b"\x01\x02" * 100  # 200 bytes = 100 frames of 16-bit mono
    wav_bytes = _wrap_wav(pcm, sample_rate=16000)

    assert wav_bytes[:4] == b"RIFF"
    assert wav_bytes[8:12] == b"WAVE"

    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getframerate() == 16000
        assert w.getsampwidth() == 2
        assert w.getnframes() == 100
        assert w.readframes(w.getnframes()) == pcm


class _FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json_data


def test_close_posts_and_delivers_transcript_synchronously(monkeypatch):
    captured = {}

    def fake_post(url, files, data, headers, timeout):
        captured["url"] = url
        captured["files"] = files
        captured["data"] = data
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _FakeResponse({"text": "hello world"})

    monkeypatch.setattr(openai_stt_module.requests, "post", fake_post)

    session = OpenAISTTSession(
        base_url="https://example.com/v1", api_key="sk-test", model="whisper-1",
    )
    received = []
    session.start(lambda text, is_final: received.append((text, is_final)))
    session.send_audio(b"\x00\x01" * 800)  # some 16kHz linear16 audio
    session.close()

    # Synchronous: the callback has already fired by the time close() returns.
    assert received == [("hello world", True)]
    assert captured["url"] == "https://example.com/v1/audio/transcriptions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert session.is_closed() is True

    # Idempotent — a second close() is a no-op, no second request fired.
    session.close()
    assert received == [("hello world", True)]


def test_close_with_no_audio_sends_no_request(monkeypatch):
    called = []
    monkeypatch.setattr(
        openai_stt_module.requests,
        "post",
        lambda *a, **kw: called.append(1) or _FakeResponse({"text": "should not happen"}),
    )

    session = OpenAISTTSession(base_url="https://example.com/v1", api_key="sk-test", model="whisper-1")
    received = []
    session.start(lambda text, is_final: received.append((text, is_final)))
    session.close()  # no send_audio() call — keepalive pre-connect that never saw speech

    assert called == []
    assert received == []
    assert session.is_closed() is True


def test_on_transcript_cb_swap_is_honored(monkeypatch):
    """voice_service.py reuses a pre-connected session by overwriting
    ``_on_transcript_cb`` directly rather than calling start() again."""
    monkeypatch.setattr(
        openai_stt_module.requests,
        "post",
        lambda *a, **kw: _FakeResponse({"text": "swapped"}),
    )

    session = OpenAISTTSession(base_url="https://example.com/v1", api_key="sk-test", model="whisper-1")

    def _original_cb(text, is_final):
        raise AssertionError("original callback should not fire after swap")

    session.start(_original_cb)

    received = []
    session._on_transcript_cb = lambda text, is_final: received.append((text, is_final))
    session.send_audio(b"\x00\x01" * 800)
    session.close()

    assert received == [("swapped", True)]


def test_provider_creates_openai_session_with_normalized_base_url():
    provider = OpenAISTT(api_key="sk-test", base_url="https://example.com", model="whisper-1", language="en")
    assert provider.available is True
    session = provider.create_session()
    assert isinstance(session, OpenAISTTSession)
    assert session._base_url == "https://example.com/v1"
    assert session._language == "en"


@pytest.mark.parametrize(
    "stt_provider, expected_cls",
    [
        ("openai", OpenAISTT),
        ("deepgram", DeepgramSTT),
        ("autonomous", AutonomousSTT),
    ],
)
def test_select_stt_provider_forced_choice(stt_provider, expected_cls):
    result = select_stt_provider(
        stt_provider=stt_provider,
        deepgram_api_key="dg-key",
        llm_api_key="llm-key",
        llm_base_url="https://llm.example.com",
        stt_api_key="",
        stt_base_url="",
    )
    assert isinstance(result, expected_cls)


def test_select_stt_provider_legacy_prefers_deepgram_when_configured():
    result = select_stt_provider(
        stt_provider="",
        deepgram_api_key="dg-key",
        llm_api_key="llm-key",
        llm_base_url="https://llm.example.com",
    )
    assert isinstance(result, DeepgramSTT)


def test_select_stt_provider_legacy_falls_back_to_autonomous():
    result = select_stt_provider(
        stt_provider="",
        deepgram_api_key="",
        llm_api_key="llm-key",
        llm_base_url="https://llm.example.com",
    )
    assert isinstance(result, AutonomousSTT)


def test_select_stt_provider_openai_falls_back_to_llm_base_url():
    result = select_stt_provider(
        stt_provider="openai",
        llm_api_key="llm-key",
        llm_base_url="https://llm.example.com",
        stt_api_key="",
        stt_base_url="",
    )
    assert isinstance(result, OpenAISTT)
    assert result._base_url == "https://llm.example.com/v1"
    assert result._api_key == "llm-key"
