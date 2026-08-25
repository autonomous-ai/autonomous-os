"""
OpenAI-compatible STT provider — batch transcription via
`POST {base_url}/audio/transcriptions` (multipart, whisper-1-style).

Verified target: oMLX (`POST /v1/audio/transcriptions` multipart -> {"text":
...}), but any OpenAI-compatible transcription endpoint works the same way —
this driver is deliberately generic, not oMLX-specific.

Unlike Deepgram/Autonomous (streaming WebSocket, partial + final results),
this is batch-only and interim-free by design: send_audio() just buffers
16kHz linear16 PCM in memory, and close() wraps the buffer as a WAV file and
POSTs it once. No connection is opened until close() actually has audio to
send.
"""

import io
import logging
import wave
from typing import Callable, List, Optional

import requests

from hal.drivers.voice.stt.provider import STTProvider, STTSession
from hal.drivers.voice.tts.openai import _ensure_openai_v1

logger = logging.getLogger("hal.voice.stt")

DEFAULT_MODEL = "whisper-1"

# Matches voice_cfg.STT_RATE — all audio handed to send_audio() has already
# been resampled to this rate by voice_service.py before it reaches us.
DEFAULT_SAMPLE_RATE = 16000
_WAV_CHANNELS = 1
_WAV_SAMPLE_WIDTH = 2  # 16-bit linear PCM

_REQUEST_TIMEOUT_S = 30


def _wrap_wav(pcm_bytes: bytes, sample_rate: int) -> bytes:
    """Wrap raw linear16 PCM in a minimal WAV container (stdlib `wave`)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(_WAV_CHANNELS)
        w.setsampwidth(_WAV_SAMPLE_WIDTH)
        w.setframerate(sample_rate)
        w.writeframes(pcm_bytes)
    return buf.getvalue()


class OpenAISTTSession(STTSession):
    """One batch STT 'session': buffer audio, transcribe once on close().

    No `send_keepalive` — deliberately not implemented. voice_service.py only
    calls it via `hasattr(session, "send_keepalive")`, and there is nothing
    to keep alive here: no connection exists until close() has audio to POST.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        language: Optional[str] = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ):
        self._base_url = base_url
        self._api_key = api_key
        self._model = model or DEFAULT_MODEL
        self._language = language
        self._sample_rate = sample_rate
        self._buffer = bytearray()
        self._closed = False
        self._on_transcript_cb: Optional[Callable[[str, bool], None]] = None

    def start(self, on_transcript: Callable[[str, bool], None]) -> bool:
        # Stored under this exact attribute name because voice_service.py
        # reuses a pre-connected (keepalive) session by overwriting it
        # directly (`stt_session._on_transcript_cb = on_transcript`) rather
        # than calling start() again. No network I/O happens here.
        self._on_transcript_cb = on_transcript
        return True

    def send_audio(self, data: bytes):
        self._buffer.extend(data)

    def close(self):
        """Synchronous flush — wraps and POSTs the buffered audio once.

        voice_service.py calls close() immediately before finalize_session(),
        so this must not return until the transcript (if any) has already
        reached on_transcript_cb; no background thread.
        """
        if self._closed:
            return
        self._closed = True
        if not self._buffer:
            # A keepalive session that never saw audio (pre-connected, closed
            # before speech started) — no-op, same as the other providers'
            # cheap close() in that case.
            return
        try:
            wav_bytes = _wrap_wav(bytes(self._buffer), self._sample_rate)
            files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
            data = {"model": self._model, "response_format": "json"}
            if self._language:
                data["language"] = self._language
            headers = {}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            resp = requests.post(
                f"{self._base_url}/audio/transcriptions",
                files=files,
                data=data,
                headers=headers,
                timeout=_REQUEST_TIMEOUT_S,
            )
            resp.raise_for_status()
            text = (resp.json() or {}).get("text", "").strip()
            if text and self._on_transcript_cb:
                self._on_transcript_cb(text, True)
        except Exception as e:
            logger.warning("OpenAI STT: transcription request failed: %s", e)

    def is_closed(self) -> bool:
        return self._closed


class OpenAISTT(STTProvider):
    """Batch STT via any OpenAI-compatible `/audio/transcriptions` endpoint."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = 1,
        model: str = DEFAULT_MODEL,
        language: Optional[str] = None,
        keywords: Optional[List[str]] = None,
    ):
        # `keywords` is accepted (unused) for call-site parity with
        # DeepgramSTT/AutonomousSTT — this is a batch endpoint with no
        # keyword-boosting parameter to forward it to.
        self._api_key = api_key
        self._base_url = _ensure_openai_v1(base_url or "")
        self._sample_rate = sample_rate
        self._model = model or DEFAULT_MODEL
        self._language = language

    def create_session(self) -> STTSession:
        return OpenAISTTSession(
            base_url=self._base_url,
            api_key=self._api_key,
            model=self._model,
            language=self._language,
            sample_rate=self._sample_rate,
        )

    @property
    def available(self) -> bool:
        return self._base_url != ""

    @property
    def name(self) -> str:
        return f"OpenAISTT({self._model})"
