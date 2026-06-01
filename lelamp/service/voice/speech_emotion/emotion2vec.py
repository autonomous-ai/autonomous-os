"""emotion2vec recognizer — POSTs WAV to dlbackend /api/dl/ser/recognize.

Mirrors `RemoteEmotionRecognizer` in the face emotion processor: stateless
HTTP wrapper, returns `None` on any transport/parse failure so the caller
can simply skip the sample.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Optional

import requests

import lelamp.config as config
from lelamp.service.sensing.crypto import CryptoSession, resolve_public_key
from lelamp.service.voice.speech_emotion.base import (
    BaseSpeechEmotionRecognizer,
    SpeechEmotionResult,
)
from lelamp.service.voice.speech_emotion.constants import DEFAULT_API_TIMEOUT_S

logger = logging.getLogger("lelamp.voice.speech_emotion.engine")


class Emotion2VecRecognizer(BaseSpeechEmotionRecognizer):
    """HTTP wrapper around dlbackend `/api/dl/ser/recognize`.

    Request body (per dlbackend README):
        {"audio_b64": "<base64 WAV>", "return_scores": false}
    Response body:
        {"label": "happy", "confidence": 0.9981, "scores": null}
    """

    def __init__(
        self,
        url: str,
        api_key: str = "",
        timeout_s: float = DEFAULT_API_TIMEOUT_S,
    ):
        self._url: str = url or ""
        self._api_key: str = api_key or ""
        self._timeout: float = timeout_s

        self._crypto: CryptoSession | None = None
        if config.DL_ENCRYPTION_ENABLED:
            public_key = resolve_public_key(config.DL_PUBLIC_KEY_URL, config.DL_API_KEY, config.DL_PUBLIC_KEY_FILE)
            if public_key is not None:
                self._crypto = CryptoSession(public_key)
                logger.info("[speech_emotion.engine] encryption enabled")
            elif config.DL_ENCRYPTION_REQUIRED:
                raise RuntimeError("Encryption required but no public key available")

    @property
    def available(self) -> bool:
        return bool(self._url)

    def recognize(self, wav_bytes: bytes) -> Optional[SpeechEmotionResult]:
        if not self._url:
            logger.warning("[speech_emotion.engine] recognize skipped — empty URL")
            return None
        if not wav_bytes:
            logger.warning("[speech_emotion.engine] recognize skipped — empty wav")
            return None
        b64 = base64.b64encode(wav_bytes).decode("ascii")
        logger.info(
            "[speech_emotion.engine] POST %s (wav=%d bytes, b64=%d chars, timeout=%.1fs)",
            self._url, len(wav_bytes), len(b64), self._timeout,
        )
        try:
            payload = {"audio_b64": b64, "return_scores": False}
            headers: dict[str, str] = {"Content-Type": "application/json"}
            if self._api_key:
                headers["X-API-Key"] = self._api_key
            if self._crypto is not None:
                resp = requests.post(
                    self._url,
                    data=self._crypto.wrap_http_request(json.dumps(payload).encode()),
                    headers=headers, timeout=self._timeout,
                )
            else:
                resp = requests.post(
                    self._url, json=payload, headers=headers, timeout=self._timeout,
                )
        except requests.RequestException as e:
            logger.warning("[speech_emotion.engine] request failed: %s", e)
            return None

        if resp.status_code != 200:
            logger.warning(
                "[speech_emotion.engine] HTTP %d: %s",
                resp.status_code, resp.text[:200],
            )
            return None

        try:
            if self._crypto is not None:
                data = json.loads(self._crypto.unwrap_http_response(resp.content))
            else:
                data = resp.json()
        except ValueError:
            logger.warning(
                "[speech_emotion.engine] non-JSON response: %s", resp.text[:200],
            )
            return None

        label = data.get("label")
        if not label:
            logger.warning(
                "[speech_emotion.engine] response missing 'label': %s",
                str(data)[:200],
            )
            return None
        confidence = float(data.get("confidence", 0.0))
        logger.info(
            "[speech_emotion.engine] response OK: label=%s confidence=%.3f",
            label, confidence,
        )
        return SpeechEmotionResult(label=label, confidence=confidence)
