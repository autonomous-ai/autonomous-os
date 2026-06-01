"""SpeechEmotionService — public orchestrator.

Receives `(user, wav_bytes, duration_s)` per utterance from voice_service,
buffers recognition results per user, periodically flushes with polarity-
bucket dedup, and POSTs `speech_emotion.detected` sensing events to Lamp.

Architecture mirrors `EmotionPerception` in the face sensing pipeline:

    submit()                            # non-blocking
        │  (queue.put_nowait)
        ▼
    worker thread  ── HTTP recognize ──▶ dlbackend /api/dl/ser/recognize
        │
        ▼
    per-user buffer[user] = [Inference, …]
        ▲
        │  (flush thread wakes every FLUSH_S)
        ▼
    flush:
        - drop neutral / empty user
        - mode label per user
        - bucket = polarity(mode)
        - TTL dedup keyed on (user, bucket) over DEDUP_WINDOW_S
        - hedged message → POST Lamp sensing event

Anti-spam guards (matched to face emotion):

    1. submit() drops audio shorter than MIN_AUDIO_S
    2. submit() drops empty user
    3. worker drops results below the per-label threshold from
       constants.CONFIDENCE_THRESHOLD_BY_LABEL (DEFAULT_CONFIDENCE_THRESHOLD
       for unlisted labels)
    4. flush drops neutral/<unk>/other labels
    5. flush dedups by (user, bucket) over DEDUP_WINDOW_S
"""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from collections import Counter
from copy import copy
from dataclasses import dataclass
from typing import Optional

import requests

from lelamp import config
from lelamp.service.voice.speech_emotion.base import (
    BaseSpeechEmotionRecognizer,
)
from lelamp.service.voice.speech_emotion.constants import (
    CONFIDENCE_THRESHOLD_BY_LABEL,
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_DEDUP_WINDOW_S,
    DEFAULT_DL_SER_ENDPOINT,
    DEFAULT_FLUSH_S,
    DEFAULT_MIN_AUDIO_S,
    DEFAULT_QUEUE_MAXSIZE,
    SENSING_EVENT_TYPE,
    SpeechEmotionLabel
)
from lelamp.service.voice.speech_emotion.emotion2vec import Emotion2VecRecognizer
from lelamp.service.voice.speech_emotion.utils import (
    bucket_for,
    format_message,
    is_neutral,
    normalize_label,
    threshold_for,
)

logger = logging.getLogger("lelamp.voice.speech_emotion")

# Resolve runtime knobs from lelamp.config with sensible fallbacks so the
# module imports cleanly even if the config hasn't been bumped yet.
_FLUSH_S: float = float(getattr(config, "SPEECH_EMOTION_FLUSH_S", DEFAULT_FLUSH_S))
_DEDUP_WINDOW_S: float = float(
    getattr(config, "SPEECH_EMOTION_DEDUP_WINDOW_S", DEFAULT_DEDUP_WINDOW_S)
)
_MIN_AUDIO_S: float = float(
    getattr(config, "SPEECH_EMOTION_MIN_AUDIO_S", DEFAULT_MIN_AUDIO_S)
)
_API_URL: str = getattr(config, "SPEECH_EMOTION_API_URL", "") or ""
_API_KEY: str = getattr(config, "SPEECH_EMOTION_API_KEY", "") or ""
_LAMP_URL: str = config.LAMP_SENSING_URL
_AUDIO_DIR: str = getattr(config, "SPEECH_EMOTION_AUDIO_DIR", "") or ""
_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]+")


@dataclass(slots=True)
class _Job:
    user: str
    wav_bytes: bytes
    duration_s: float


@dataclass(slots=True)
class _Inference:
    user: str
    label: SpeechEmotionLabel
    confidence: float
    duration_s: float
    ts: float
    audio_path: str = ""


def _build_default_recognizer() -> BaseSpeechEmotionRecognizer:
    """Compose URL from DL_BACKEND_URL + DL_SER_ENDPOINT if not preset."""
    url = _API_URL
    if not url and config.DL_BACKEND_URL:
        endpoint = getattr(config, "DL_SER_ENDPOINT", DEFAULT_DL_SER_ENDPOINT)
        url = (
            config.DL_BACKEND_URL.rstrip("/")
            + "/"
            + endpoint.strip("/")
        )
    return Emotion2VecRecognizer(url=url, api_key=_API_KEY or config.DL_API_KEY)


class SpeechEmotionService:
    """Init once per process; call submit() per utterance.

    Spawns two daemon threads when the recognizer is available — worker
    (drains the submission queue, runs HTTP recognize) and flush (drains
    the per-user buffer every FLUSH_S, dedups, sends to Lamp). Both shut
    down when stop() is called.
    """

    def __init__(
        self,
        recognizer: Optional[BaseSpeechEmotionRecognizer] = None,
        *,
        flush_s: float = _FLUSH_S,
        dedup_window_s: float = _DEDUP_WINDOW_S,
        min_audio_s: float = _MIN_AUDIO_S,
        lamp_url: str = _LAMP_URL,
        audio_dir: str = _AUDIO_DIR,
        queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
    ):
        self._recognizer: BaseSpeechEmotionRecognizer = (
            recognizer if recognizer is not None else _build_default_recognizer()
        )
        self._flush_s: float = flush_s
        self._dedup_window_s: float = dedup_window_s
        self._min_audio_s: float = min_audio_s
        self._lamp_url: str = lamp_url
        self._audio_dir: str = audio_dir

        # mutable state — guarded by _lock
        self._lock: threading.RLock = threading.RLock()
        self._buffer: dict[str, list[_Inference]] = {}
        self._last_sent_by_key: dict[tuple[str, str], float] = {}
        self._last_flush_ts: float = 0.0

        self._stop_event: threading.Event = threading.Event()
        self._jobs: queue.Queue[Optional[_Job]] = queue.Queue(maxsize=queue_maxsize)
        self._worker_thread: Optional[threading.Thread] = None
        self._flush_thread: Optional[threading.Thread] = None

        if self.available:
            if self._audio_dir:
                try:
                    os.makedirs(self._audio_dir, exist_ok=True)
                except OSError as e:
                    logger.warning(
                        "[speech_emotion] audio_dir mkdir failed (%s): %s — "
                        "POST will carry empty audio field",
                        self._audio_dir, e,
                    )
                    self._audio_dir = ""
            self._start_workers()
            logger.info(
                "[speech_emotion] SERVICE STARTED — flush=%.1fs dedup=%.1fs "
                "min_audio=%.1fs per-label thresholds=%s default=%.2f "
                "lamp_url=%s audio_dir=%s recognizer=%s",
                flush_s, dedup_window_s, min_audio_s,
                CONFIDENCE_THRESHOLD_BY_LABEL, DEFAULT_CONFIDENCE_THRESHOLD,
                self._lamp_url, self._audio_dir or "<disabled>",
                type(self._recognizer).__name__,
            )
        else:
            logger.warning(
                "[speech_emotion] SERVICE IDLE — recognizer unavailable "
                "(missing DL_BACKEND_URL or endpoint config). submit() will "
                "be a no-op until restart."
            )

    # --- public API -------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._recognizer is not None and self._recognizer.available

    def submit(self, user: str, wav_bytes: bytes, duration_s: float) -> None:
        """Non-blocking. Drops the sample (and logs) when:
          - service is disabled / recognizer unavailable
          - user is empty (no subject to attribute emotion to)
          - audio is empty or shorter than MIN_AUDIO_S
          - worker queue is full (back-pressure — caller should not retry)

        The caller passes the SAME wav_bytes used for speaker recognition;
        no defensive copy is needed because bytes are immutable in Python.
        """
        logger.info(
            "[speech_emotion] submit() called: user=%r duration=%.2fs wav=%d bytes",
            user, duration_s, len(wav_bytes) if wav_bytes else 0,
        )
        if not self.available:
            logger.info("[speech_emotion] DROP submit — service unavailable")
            return
        norm_user = normalize_label(user)
        if not norm_user:
            logger.info("[speech_emotion] DROP submit — user normalized to empty")
            return
        if not wav_bytes:
            logger.info("[speech_emotion] DROP submit — wav_bytes empty")
            return
        if duration_s < self._min_audio_s:
            logger.info(
                "[speech_emotion] DROP submit — duration=%.2fs < min=%.2fs",
                duration_s, self._min_audio_s,
            )
            return

        job = _Job(user=norm_user, wav_bytes=wav_bytes, duration_s=duration_s)
        try:
            self._jobs.put_nowait(job)
            logger.info(
                "[speech_emotion] ENQUEUED — user=%r queue_size=%d",
                norm_user, self._jobs.qsize(),
            )
        except queue.Full:
            logger.warning(
                "[speech_emotion] DROP submit — worker queue full (size=%d)",
                self._jobs.qsize(),
            )

    def stop(self) -> None:
        """Signal worker + flush threads to exit. Idempotent."""
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        try:
            self._jobs.put_nowait(None)
        except queue.Full:
            pass

    def to_dict(self) -> dict:
        """Diagnostic snapshot — mirrors EmotionPerception.to_dict shape."""
        with self._lock:
            return {
                "type": "speech_emotion",
                "available": self.available,
                "buffered_users": len(self._buffer),
                "dedup_keys": len(self._last_sent_by_key),
                "queue_size": self._jobs.qsize(),
                "last_flush_ts": self._last_flush_ts,
            }

    # --- worker thread ----------------------------------------------------
    def _start_workers(self) -> None:
        self._worker_thread = threading.Thread(
            target=self._worker_loop, name="speech-emotion-worker", daemon=True,
        )
        self._flush_thread = threading.Thread(
            target=self._flush_loop, name="speech-emotion-flush", daemon=True,
        )
        self._worker_thread.start()
        self._flush_thread.start()

    def _worker_loop(self) -> None:
        logger.info("[speech_emotion] worker thread READY")
        while not self._stop_event.is_set():
            try:
                job = self._jobs.get(timeout=1.0)
            except queue.Empty:
                continue
            if job is None:
                logger.info("[speech_emotion] worker thread received stop sentinel")
                break
            try:
                self._process_job(job)
            except Exception:
                logger.exception("[speech_emotion] worker loop error")
        logger.info("[speech_emotion] worker thread EXIT")

    def _process_job(self, job: _Job) -> None:
        t0 = time.time()
        logger.info(
            "[speech_emotion] worker -> recognize: user=%r duration=%.2fs",
            job.user, job.duration_s,
        )
        result = self._recognizer.recognize(job.wav_bytes)
        elapsed = time.time() - t0
        if result is None:
            logger.warning(
                "[speech_emotion] DROP — recognizer returned None for user=%r "
                "(took %.2fs; check DL backend reachability / response shape)",
                job.user, elapsed,
            )
            return
        logger.info(
            "[speech_emotion] recognize OK: user=%r label=%s confidence=%.3f (took %.2fs)",
            job.user, result.label, result.confidence, elapsed,
        )
        label = SpeechEmotionLabel(normalize_label(result.label))
        label_threshold = threshold_for(label)
        if result.confidence < label_threshold:
            logger.info(
                "[speech_emotion] DROP — low confidence: %s %.3f < %.2f",
                label, result.confidence, label_threshold,
            )
            return

        inf_ts = time.time()
        audio_path = self._persist_wav(job.wav_bytes, job.user, label, inf_ts)
        inf = _Inference(
            user=job.user,
            label=label,
            confidence=result.confidence,
            duration_s=job.duration_s,
            ts=inf_ts,
            audio_path=audio_path,
        )
        with self._lock:
            self._buffer.setdefault(job.user, []).append(inf)
            buf_len = len(self._buffer[job.user])
        logger.info(
            "[speech_emotion] BUFFERED — user=%r label=%s conf=%.3f buf_len=%d audio=%s",
            job.user, inf.label, inf.confidence, buf_len, audio_path or "<none>",
        )

    def _persist_wav(
        self,
        wav_bytes: bytes,
        user: str,
        label: SpeechEmotionLabel,
        ts: float,
    ) -> str:
        """Write the WAV buffer to disk and return the path. Empty string on
        skip/failure (audio_dir disabled or I/O error) — caller must tolerate.
        """
        if not self._audio_dir:
            return ""
        safe_user = _SAFE_NAME_RE.sub("_", user) or "unknown"
        safe_label = _SAFE_NAME_RE.sub("_", label.value) or "unknown"
        filename = f"{int(ts * 1000)}_{safe_user}_{safe_label}.wav"
        path = os.path.join(self._audio_dir, filename)
        try:
            with open(path, "wb") as f:
                f.write(wav_bytes)
        except OSError as e:
            logger.warning(
                "[speech_emotion] persist wav failed (%s): %s", path, e,
            )
            return ""
        return path

    # --- flush thread -----------------------------------------------------

    def _flush_loop(self) -> None:
        logger.info(
            "[speech_emotion] flush thread READY (interval=%.1fs)", self._flush_s,
        )
        while not self._stop_event.is_set():
            # wait() returns True if the stop event fires during the wait —
            # use that as the exit signal to avoid one extra flush at shutdown.
            if self._stop_event.wait(self._flush_s):
                logger.info("[speech_emotion] flush thread EXIT")
                return
            try:
                self._flush_once()
            except Exception:
                logger.exception("[speech_emotion] flush failed")

    def _flush_once(self) -> None:
        cur_ts = time.time()
        with self._lock:
            if not self._buffer:
                logger.debug("[speech_emotion] flush tick: buffer empty")
                return
            buf = copy(self._buffer)
            self._buffer.clear()
            self._last_flush_ts = cur_ts
            # Prune expired dedup entries (oldest TTL window).
            cutoff = cur_ts - self._dedup_window_s
            before = len(self._last_sent_by_key)
            self._last_sent_by_key = {
                k: ts for k, ts in self._last_sent_by_key.items() if ts >= cutoff
            }
            pruned = before - len(self._last_sent_by_key)

        logger.info(
            "[speech_emotion] flush tick: users=%d dedup_keys=%d (pruned=%d)",
            len(buf), len(self._last_sent_by_key), pruned,
        )
        for user, inferences in buf.items():
            if not user or not inferences:
                continue
            self._flush_user(user, inferences, cur_ts)

    def _flush_user(
        self, user: str, inferences: list[_Inference], cur_ts: float,
    ) -> None:
        logger.info(
            "[speech_emotion] flushing user=%r samples=%d labels=[%s]",
            user, len(inferences),
            ", ".join(inf.label for inf in inferences),
        )
        non_neutral = [inf for inf in inferences if not is_neutral(inf.label)]
        if not non_neutral:
            logger.info(
                "[speech_emotion] DROP — %s: all %d samples are neutral/<unk>/other",
                user, len(inferences),
            )
            return

        counts = Counter(inf.label for inf in non_neutral)
        dominant_label, _ = counts.most_common(1)[0]
        dom_inferences = [inf for inf in non_neutral if inf.label == dominant_label]
        avg_confidence = sum(inf.confidence for inf in dom_inferences) / len(
            dom_inferences
        )
        bucket = bucket_for(dominant_label)
        latest_audio_path = max(dom_inferences, key=lambda i: i.ts).audio_path
        logger.info(
            "[speech_emotion] mode for user=%r: label=%s avg_conf=%.3f bucket=%s audio=%s",
            user, dominant_label, avg_confidence, bucket,
            latest_audio_path or "<none>",
        )

        key = (user, bucket)
        with self._lock:
            last_ts = self._last_sent_by_key.get(key)
            if last_ts is not None and (cur_ts - last_ts) < self._dedup_window_s:
                logger.info(
                    "[speech_emotion] DROP — dedup: user=%r bucket=%s "
                    "(last sent %.1fs ago, window=%.1fs)",
                    user, bucket, cur_ts - last_ts, self._dedup_window_s,
                )
                return
            self._last_sent_by_key[key] = cur_ts

        message = format_message(dominant_label, avg_confidence, bucket)
        logger.info(
            "[speech_emotion] EMIT — user=%r message=%r audio=%s",
            user, message, latest_audio_path or "<none>",
        )
        self._send_to_lamp(
            message=message, user=user, audio_path=latest_audio_path,
        )

    # --- transport --------------------------------------------------------

    def _send_to_lamp(
        self, *, message: str, user: str, audio_path: str = "",
    ) -> None:
        """POST sensing event to Lamp with 3x retry on connection error / 503.

        Same shape as voice_service.send_to_lamp but carries `current_user`
        explicitly so the Lamp sensing handler doesn't have to look it up.
        ``audio_path`` is the on-disk path of the latest WAV that produced
        the dominant label this flush; empty when persistence is disabled
        or the write failed.
        """
        if not self._lamp_url:
            logger.warning(
                "[speech_emotion] send_to_lamp skipped — empty lamp_url"
            )
            return
        payload = {
            "type": SENSING_EVENT_TYPE,
            "message": message,
            "current_user": user,
            "audio": audio_path,
        }
        logger.info(
            "[speech_emotion] POST -> %s payload.user=%r payload.type=%s audio=%s",
            self._lamp_url, user, SENSING_EVENT_TYPE, audio_path or "<none>",
        )
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(self._lamp_url, json=payload, timeout=5)
            except requests.ConnectionError as e:
                if attempt < max_retries:
                    logger.warning(
                        "[speech_emotion] Lamp unreachable (attempt %d/%d), "
                        "retry in 2s",
                        attempt, max_retries,
                    )
                    time.sleep(2)
                    continue
                logger.warning(
                    "[speech_emotion] Lamp unreachable after %d attempts: %s",
                    max_retries, e,
                )
                return
            except requests.RequestException as e:
                logger.warning("[speech_emotion] Lamp POST failed: %s", e)
                return

            if resp.status_code == 503 and attempt < max_retries:
                logger.warning(
                    "[speech_emotion] Lamp 503, retry %d/%d in 2s",
                    attempt, max_retries,
                )
                time.sleep(2)
                continue
            if resp.status_code != 200:
                logger.warning(
                    "[speech_emotion] Lamp returned %d: %s",
                    resp.status_code, resp.text[:200],
                )
                return
            logger.info(
                "[speech_emotion] SENT -> Lamp 200 OK (attempt=%d): %s",
                attempt, message,
            )
            return
