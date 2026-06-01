"""emotion2vec recognizer — POSTs WAV to dlbackend /api/dl/ser/recognize.

Mirrors `RemoteEmotionRecognizer` in the face emotion processor: stateless
HTTP wrapper, returns `None` on any transport/parse failure so the caller
can simply skip the sample.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Optional

import numpy as np
import numpy.typing as npt
import requests

import lelamp.config as config
from lelamp.service.sensing.crypto import CryptoSession, resolve_public_key
from lelamp.service.voice.speech_emotion.base import (
    BaseSpeechEmotionRecognizer,
    SpeechEmotionResult,
)
from lelamp.service.voice.speech_emotion.constants import (
    DEFAULT_API_TIMEOUT_S,
    PREFILTER_FRAME_MS,
    PREFILTER_MIN_TRIMMED_S,
    PREFILTER_MIN_VOICED_RATIO,
    PREFILTER_MIN_VOICED_S,
    PREFILTER_PAD_MS,
    PREFILTER_SAMPLE_RATE,
    PREFILTER_SILERO_CHUNK_SAMPLES,
    PREFILTER_SILERO_CONTEXT_SAMPLES,
    PREFILTER_SILERO_THRESHOLD,
    PREFILTER_TRIM_RMS,
    PREFILTER_VAD_FALLBACK_MIN_VOICED_S,
    PREFILTER_VAD_MIN_VOICED_S,
    PREFILTER_VOICED_RMS,
)
from lelamp.service.voice.speech_emotion.utils import (
    compute_trim_and_voiced,
    pcm16_to_wav,
    wav_to_pcm16,
)

logger = logging.getLogger("lelamp.voice.speech_emotion.engine")

# Silero VAD ONNX model — voice_service.py loads its own InferenceSession
# from the same file for the per-frame VAD trigger. Path kept in sync with
# voice_service.py:_SILERO_MODEL_PATH; if either side moves the model file
# the other one will silently fall back to its RMS-only path.
_SILERO_MODEL_PATH: Path = (
    Path(__file__).resolve().parent.parent / "resources" / "silero_vad.onnx"
)


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

        self._silero = self._load_silero()

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
        filtered = self.prefilter(wav_bytes)
        if filtered is None:
            return None
        wav_bytes = filtered
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

    # --- Prefilter ---------------------------------------------------------
    # Two-tier gate to suppress long-but-sparse audio that the model would
    # otherwise classify with a spurious high-confidence label:
    #   Stage 1 (RMS): head/tail trim + voiced duration/ratio thresholds.
    #   Stage 2 (Silero VAD): truly-speech-like duration on the trimmed
    #                         buffer to reject TV/music/clap/noise.
    # Returns the re-encoded TRIMMED WAV (so the recognizer sees the cleaner
    # buffer too), or None when the sample should be dropped entirely.

    def prefilter(self, wav_bytes: bytes) -> Optional[bytes]:
        """Apply the RMS + Silero prefilter to ``wav_bytes``.

        Returns the trimmed WAV ready for recognition, or ``None`` to drop.
        Logs the decision and the metrics that drove it so thresholds can
        be tuned from production logs without re-instrumenting.
        """
        try:
            samples, sample_rate = wav_to_pcm16(wav_bytes)
        except Exception as e:
            logger.warning("[prefilter] DROP — wav decode failed: %s", e)
            return None
        if samples.size == 0 or sample_rate <= 0:
            logger.info("[prefilter] DROP — empty audio after decode")
            return None
        if sample_rate != PREFILTER_SAMPLE_RATE:
            logger.info(
                "[prefilter] DROP — unexpected sample_rate=%d (expected %d)",
                sample_rate, PREFILTER_SAMPLE_RATE,
            )
            return None
        total_s = samples.size / sample_rate

        # Stage 1 — single-pass RMS: trim head/tail AND count voiced frames
        # from the same RMS envelope (avoids recomputing per-frame energy).
        trimmed, voiced_s, ratio = compute_trim_and_voiced(
            samples, sample_rate,
            PREFILTER_TRIM_RMS, PREFILTER_VOICED_RMS,
            PREFILTER_FRAME_MS, PREFILTER_PAD_MS,
        )
        trimmed_s = trimmed.size / sample_rate
        if trimmed_s < PREFILTER_MIN_TRIMMED_S:
            logger.info(
                "[prefilter] DROP — trim too short: total=%.2fs trim=%.2fs < %.2fs",
                total_s, trimmed_s, PREFILTER_MIN_TRIMMED_S,
            )
            return None

        if voiced_s < PREFILTER_MIN_VOICED_S:
            logger.info(
                "[prefilter] DROP — RMS voiced=%.2fs < %.2fs "
                "(total=%.2fs trim=%.2fs ratio=%.0f%%)",
                voiced_s, PREFILTER_MIN_VOICED_S,
                total_s, trimmed_s, ratio * 100,
            )
            return None
        if ratio < PREFILTER_MIN_VOICED_RATIO:
            logger.info(
                "[prefilter] DROP — RMS voiced_ratio=%.0f%% < %.0f%% "
                "(voiced=%.2fs trim=%.2fs)",
                ratio * 100, PREFILTER_MIN_VOICED_RATIO * 100,
                voiced_s, trimmed_s,
            )
            return None

        # Stage 2 — Silero VAD on the trimmed buffer
        silero_voiced_s = self._silero_voiced_seconds(trimmed, sample_rate)
        if silero_voiced_s is None:
            # Silero unavailable — fall back to a stricter RMS bar so we
            # don't ship "may-be-noise" audio to the model unchecked.
            if voiced_s < PREFILTER_VAD_FALLBACK_MIN_VOICED_S:
                logger.info(
                    "[prefilter] DROP — Silero off and RMS voiced=%.2fs < fallback %.2fs",
                    voiced_s, PREFILTER_VAD_FALLBACK_MIN_VOICED_S,
                )
                return None
            logger.info(
                "[prefilter] PASS (Silero off, RMS fallback) "
                "total=%.2fs trim=%.2fs rms_voiced=%.2fs (%.0f%%)",
                total_s, trimmed_s, voiced_s, ratio * 100,
            )
        elif silero_voiced_s < PREFILTER_VAD_MIN_VOICED_S:
            logger.info(
                "[prefilter] DROP — Silero voiced=%.2fs < %.2fs "
                "(total=%.2fs trim=%.2fs rms_voiced=%.2fs ratio=%.0f%%)",
                silero_voiced_s, PREFILTER_VAD_MIN_VOICED_S,
                total_s, trimmed_s, voiced_s, ratio * 100,
            )
            return None
        else:
            logger.info(
                "[prefilter] PASS total=%.2fs trim=%.2fs "
                "rms_voiced=%.2fs (%.0f%%) silero=%.2fs",
                total_s, trimmed_s, voiced_s, ratio * 100, silero_voiced_s,
            )

        try:
            return pcm16_to_wav(trimmed, sample_rate)
        except Exception as e:
            logger.warning(
                "[prefilter] re-encode failed (%s) — sending original wav", e,
            )
            return wav_bytes

    def _load_silero(self) -> Optional[object]:
        """Lazy-load Silero ONNX. Returns None on any failure (model file
        missing, onnxruntime broken, ABI mismatch) — caller treats None as
        "Silero unavailable" and falls back to a stricter RMS bar.
        """
        if not _SILERO_MODEL_PATH.exists():
            logger.info(
                "[prefilter] Silero model not found at %s — RMS-only prefilter",
                _SILERO_MODEL_PATH,
            )
            return None
        try:
            import onnxruntime as ort
            sess_opts = ort.SessionOptions()
            sess_opts.intra_op_num_threads = 1
            sess_opts.inter_op_num_threads = 1
            sess_opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            session = ort.InferenceSession(
                str(_SILERO_MODEL_PATH),
                sess_options=sess_opts,
                providers=["CPUExecutionProvider"],
            )
            logger.info(
                "[prefilter] Silero VAD loaded (threshold=%.2f, min_voiced=%.1fs)",
                PREFILTER_SILERO_THRESHOLD, PREFILTER_VAD_MIN_VOICED_S,
            )
            return session
        except Exception as e:
            logger.warning(
                "[prefilter] Silero load failed (%s) — RMS-only prefilter", e,
            )
            return None

    def _silero_voiced_seconds(
        self, samples: npt.NDArray[np.int16], sample_rate: int,
    ) -> Optional[float]:
        """Sum the duration of Silero-positive chunks across ``samples``.

        Silero is stateful; the LSTM ``state`` and the 64-sample ``context``
        are local to this call and rebuilt from zeros each time, so
        independent prefilter invocations never bleed into each other.
        Returns ``None`` if Silero is unavailable or inference fails —
        callers treat that as the "fall back to a stricter RMS bar" signal.
        """
        if self._silero is None:
            return None
        chunk = PREFILTER_SILERO_CHUNK_SAMPLES
        ctx_len = PREFILTER_SILERO_CONTEXT_SAMPLES
        try:
            audio_norm = samples.astype(np.float32) / 32768.0
            state = np.zeros((2, 1, 128), dtype=np.float32)
            context = np.zeros((1, ctx_len), dtype=np.float32)
            voiced_chunks = 0
            total_chunks = 0
            sr_arr = np.array(sample_rate, dtype=np.int64)
            for i in range(0, audio_norm.size, chunk):
                seg = audio_norm[i:i + chunk]
                if seg.size < chunk:
                    seg = np.pad(seg, (0, chunk - seg.size))
                x = np.concatenate([context, seg.reshape(1, -1)], axis=1)
                out = self._silero.run(
                    None,
                    {"input": x, "state": state, "sr": sr_arr},
                )
                prob = float(out[0][0][0])
                state = out[1]
                context = x[:, -ctx_len:]
                total_chunks += 1
                if prob >= PREFILTER_SILERO_THRESHOLD:
                    voiced_chunks += 1
            voiced_s = voiced_chunks * (chunk / float(sample_rate))
            logger.debug(
                "[prefilter] Silero stats: voiced_chunks=%d/%d voiced=%.2fs",
                voiced_chunks, total_chunks, voiced_s,
            )
            return voiced_s
        except Exception as e:
            logger.warning("[prefilter] Silero inference failed: %s", e)
            return None
