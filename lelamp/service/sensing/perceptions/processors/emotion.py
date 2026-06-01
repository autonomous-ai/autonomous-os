import base64
import json
import logging
import os
import threading
import time
from collections import Counter
from copy import copy
from dataclasses import dataclass
from typing import Any

import cv2
import requests
from typing_extensions import override

import lelamp.config as config
from lelamp.service.sensing.crypto import CryptoSession, resolve_public_key
from lelamp.service.sensing.perceptions.models import (
    Face,
    FaceDetectionData,
)
from lelamp.service.sensing.perceptions.typing import SendEventCallable
from lelamp.service.sensing.perceptions.utils import PerceptionStateObservers
from lelamp.service.sensing.presence_service import PresenceState, PresenseService

from .base import Perception

logger = logging.getLogger(__name__)

EMOTIONS = [
    "Neutral",
    "Happy",
    "Sad",
    "Surprise",
    "Fear",
    "Disgust",
    "Anger",
    "Contempt",
]

# Phase 2 bucket dedup: collapse fine-grained labels into polarity buckets.
# Dedup key is (user, bucket) so cross-bucket flips like Fear↔Happy still
# fire (different buckets) but within-bucket noise like Fear↔Sad↔Anger
# collapses to a single "negative" event per 5-min window.
# Outbound message text stays raw ("Emotion detected: Sad.") — variant A,
# minimal-risk: no downstream parsing changes.
EMOTION_BUCKETS = {
    "Happy": "positive",
    "Surprise": "positive",
    "Sad": "negative",
    "Fear": "negative",
    "Anger": "negative",
    "Disgust": "negative",
    "Contempt": "negative",
}


class RemoteEmotionRecognizer:
    """Calls the dlbackend HTTP emotion-recognize endpoint for a single face crop."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        threshold: float = config.EMOTION_CONFIDENCE_THRESHOLD,
        timeout: float = 10.0,
    ):
        self._url: str = (
            base_url.rstrip("/") + "/" + config.DL_EMOTION_RECOGNIZE_ENDPOINT.strip("/")
            if base_url
            else ""
        )
        self._api_key: str = api_key
        self._threshold: float = threshold
        self._timeout: float = timeout
        self._crypto: CryptoSession | None = None

        if config.DL_ENCRYPTION_ENABLED:
            self._setup_crypto()

    def _setup_crypto(self) -> None:
        """Initialize crypto session for HTTP encryption."""
        public_key = resolve_public_key(config.DL_PUBLIC_KEY_URL, config.DL_API_KEY, config.DL_PUBLIC_KEY_FILE)
        if public_key is None:
            if config.DL_ENCRYPTION_REQUIRED:
                raise RuntimeError("Encryption required but no public key available")
            logger.warning("[emotion] encryption enabled but no public key — plaintext fallback")
            return
        self._crypto = CryptoSession(public_key)
        logger.info("[emotion] encryption session initialized")

    def _img2b64(self, frame: cv2.typing.MatLike) -> str:
        _, buf = cv2.imencode(".jpg", frame)
        return base64.b64encode(buf.tobytes()).decode()

    def recognize(self, face_crop: cv2.typing.MatLike) -> dict[str, Any] | None:
        """Send a face crop to the emotion-recognize endpoint.

        Returns dict with keys: emotion, confidence, valence, arousal.
        Returns None if unavailable or no detection above threshold.
        """
        if not self._url:
            return None

        try:
            plain_body = json.dumps({
                "image_b64": self._img2b64(face_crop),
                "threshold": self._threshold,
            }).encode()

            if self._crypto is not None:
                resp = requests.post(
                    self._url,
                    data=self._crypto.wrap_http_request(plain_body),
                    headers={"X-API-Key": self._api_key, "Content-Type": "application/json"},
                    timeout=self._timeout,
                )
            else:
                resp = requests.post(
                    self._url,
                    data=plain_body,
                    headers={"X-API-Key": self._api_key, "Content-Type": "application/json"},
                    timeout=self._timeout,
                )

            if resp.status_code != 200:
                logger.warning(
                    "[activity.emotion] HTTP %d: %s", resp.status_code, resp.text
                )
                return None

            if self._crypto is not None:
                resp_body = self._crypto.unwrap_http_response(resp.content)
                detections = json.loads(resp_body).get("detections", [])
            else:
                detections = resp.json().get("detections", [])
            if not detections:
                return None

            # Return the top detection (highest confidence)
            top = max(detections, key=lambda d: d["confidence"])
            return top
        except requests.RequestException as e:
            logger.warning("[activity.emotion] request failed: %s", e)
            return None


@dataclass
class EmotionData:
    frame: cv2.typing.MatLike
    face: Face
    emotion: str
    confidence: float


class EmotionPerception(Perception[FaceDetectionData]):
    """Detects facial emotions via face recognizer callback + dlbackend HTTP.

    Registers a callback with FaceRecognizer. When a face is detected,
    sends the face crop to the emotion-recognize HTTP endpoint. Buffers
    results per-person and flushes aggregated emotion events periodically.
    """

    def __init__(
        self,
        perception_state: PerceptionStateObservers,
        send_event: SendEventCallable,
        presense_service: PresenseService | None,
        base_url: str = config.DL_BACKEND_URL,
        api_key: str = config.DL_API_KEY,
    ):
        super().__init__(perception_state, send_event)

        self._presence_service: PresenseService | None = presense_service
        self._base_url: str = base_url
        self._api_key: str = api_key

        self._recognizer: RemoteEmotionRecognizer = RemoteEmotionRecognizer(
            base_url=base_url,
            api_key=api_key,
            threshold=config.EMOTION_CONFIDENCE_THRESHOLD,
        )

        self._last_detection_time: float | None = None
        self._last_emotion: str | None = None

        # Lock protects all mutable state below
        self._state_lock: threading.RLock = threading.RLock()

        # Buffer per person — flushed every EMOTION_FLUSH_S
        self._flush_interval: float = config.EMOTION_FLUSH_S
        self._last_flush_ts: float = 0.0
        # {person_id: [emotion_str, ...]}
        self._emotion_buffer: dict[str, list[EmotionData]] = {}

        # Dedup: TTL map per (current_user, emotion) — repeated key inside
        # window dropped even if other emotions were sent in between.
        # Last-key-only dedup let alternating sad/fear/sad/fear bypass the
        # window and spam the agent queue every flush.
        self._last_sent_by_key: dict[tuple[str, str], float] = {}
        self._last_sent_key: tuple[str, str] | None = None  # debug/to_dict
        self._last_sent_ts: float = 0.0  # debug/to_dict
        self._dedup_window_s: float = config.EMOTION_DEDUP_WINDOW_S

    def _process_face(
        self,
        frame: cv2.typing.MatLike,
        face: Face,
    ) -> None:
        """Crop face, send to emotion backend, buffer result."""

        h, w = frame.shape[:2]
        # bbox is [x1, y1, x2, y2] from InsightFace
        x1, y1, x2, y2 = face.bbox

        # Clamp to frame bounds
        x1 = max(0, x1)
        y1 = max(0, y1)
        x2 = min(w, x2)
        y2 = min(h, y2)
        if x2 <= x1 or y2 <= y1:
            return

        face_crop = frame[y1:y2, x1:x2]

        try:
            result = self._recognizer.recognize(face_crop)
        except Exception:
            logger.exception("[activity.emotion] recognize error")
            return

        if result is None:
            return

        emotion = result["emotion"]
        confidence = result["confidence"]

        if self._presence_service:
            self._presence_service.on_motion()

        with self._state_lock:
            self._last_detection_time = time.time()
            self._last_emotion = emotion

            if face.person_id not in self._emotion_buffer:
                self._emotion_buffer[face.person_id] = []

            self._emotion_buffer[face.person_id].append(
                EmotionData(
                    frame=frame,
                    face=face,
                    emotion=emotion,
                    confidence=confidence * face.confidence,
                )
            )

        logger.debug(
            "[activity.emotion] %s: %s (%.2f)", face.person_id, emotion, confidence
        )

    @override
    def cleanup(self) -> None:
        pass

    @override
    def _check_impl(self, data: FaceDetectionData) -> None:
        """Only used for periodic flush — actual detection is callback-driven."""
        if data.frame is not None:
            logger.debug("[emotion] processing %d face(s)", len(data.faces))
            for f in data.faces:
                self._process_face(data.frame, f)
        else:
            logger.debug("[emotion] frame is None, skipping detection")

        self._flush_buffer()

    def _save_annotated(
        self,
        frame: cv2.typing.MatLike,
        bbox: list[int],
        emotion: str,
        confidence: float,
    ) -> cv2.typing.MatLike | None:
        """Draw annotation and save to snapshot dir. Rotates old files."""
        try:
            os.makedirs(config.EMOTION_SNAPSHOT_DIR, exist_ok=True)

            vis = frame.copy()
            x1, y1, x2, y2 = bbox
            _ = cv2.rectangle(vis, (x1, y1), (x2, y2), (0, 255, 0), 2)
            label = f"{emotion} {confidence:.2f}"
            _ = cv2.putText(
                vis,
                label,
                (x1, y1 - 10),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 255, 0),
                2,
            )

            return vis
        except Exception as e:
            logger.debug("[activity.emotion] snapshot save failed: %s", e)
            return None

    def _flush_buffer(self) -> None:
        with self._state_lock:
            if not self._emotion_buffer:
                return

            cur_ts = time.time()
            if (cur_ts - self._last_flush_ts) < self._flush_interval:
                return

            buffer = copy(self._emotion_buffer)
            self._emotion_buffer.clear()
            self._last_flush_ts = cur_ts

        if (
            self._presence_service is not None
            and self._presence_service.state != PresenceState.PRESENT
        ):
            logger.info(
                "[activity.emotion] skipping — no presence (presence=%s)",
                self._presence_service.state,
            )
            return

        # Dedup key uses the global current_user (same source of truth as
        # MotionPerception + reset_dedup). Per-face person_id is too noisy
        # ('?' / 'stranger_17' / 'stranger_18' all flip the key on every
        # frame and bypass dedup). Skip entirely when current_user is "":
        # nobody is in scene (no friend, no stranger within forget window)
        # so there's no subject to attribute emotion to.
        current_user = self._perception_state.current_user.data or ""
        if not current_user:
            logger.info("[activity.emotion] skipping — no current_user (scene empty)")
            return

        # Prune expired entries from the TTL map once per flush.
        cutoff = cur_ts - self._dedup_window_s
        with self._state_lock:
            self._last_sent_by_key = {
                k: ts for k, ts in self._last_sent_by_key.items() if ts >= cutoff
            }

        # Process each person's emotions
        for person_id, emotion_data_list in buffer.items():
            if emotion_data_list:
                logger.info(
                    "[activity.emotion] %s raw: %s",
                    person_id,
                    ", ".join([d.emotion for d in emotion_data_list]),
                )

            # Skip Neutral
            non_neutral = [
                (ed.emotion, ed.confidence)
                for ed in emotion_data_list
                if ed.emotion != "Neutral"
            ]
            if not non_neutral:
                continue

            counts = Counter(e for e, _ in non_neutral)
            dominant_emotion, _ = counts.most_common(1)[0]

            # Average confidence over instances of the dominant label only —
            # other labels' confidences would dilute it.
            dom_confidences = [c for e, c in non_neutral if e == dominant_emotion]
            avg_confidence = sum(dom_confidences) / len(dom_confidences)

            snapshots = [
                self._save_annotated(ed.frame, ed.face.bbox, ed.emotion, ed.confidence)
                for ed in emotion_data_list
                if ed.emotion == dominant_emotion
            ]
            snapshots = [s for s in snapshots if s is not None]

            # Phase 2: dedup by polarity bucket, not raw label. Fear↔Sad
            # ↔Anger noise within the same bucket collapses to one event
            # per window; cross-bucket flips (Fear→Happy) still fire as a
            # genuine mood change. "other" bucket catches any label not in
            # EMOTION_BUCKETS so unknown emotions still self-dedup.
            bucket = EMOTION_BUCKETS.get(dominant_emotion, "other")

            # Hedge prevents LLM over-commit on noisy FER reads. Raw
            # "Emotion detected: <Name>." prefix kept for skill parser.
            hedge = {
                "negative": "do not assume the user is distressed",
                "positive": "do not over-celebrate",
            }.get(bucket, "do not over-react")
            message = (
                f"Emotion detected: {dominant_emotion}. "
                f"(weak camera cue; confidence={avg_confidence:.2f}; "
                f"bucket={bucket}; treat as uncertain, {hedge}.)"
            )

            key = (current_user, bucket)
            with self._state_lock:
                last_ts = self._last_sent_by_key.get(key)
                if last_ts is not None and (cur_ts - last_ts) < self._dedup_window_s:
                    logger.info(
                        "[activity.emotion] dedup drop: %s bucket=%s (key seen %.1fs ago)",
                        message,
                        bucket,
                        cur_ts - last_ts,
                    )
                    continue
                self._last_sent_by_key[key] = cur_ts
                self._last_sent_key = key
                self._last_sent_ts = cur_ts

            logger.info("[activity.emotion] flushing: %s", message)
            self._send_event("emotion.detected", message, "emotion", snapshots, None)

    def reset_dedup(self, new_user: str = "") -> None:
        """Clear the outbound dedup state only if the visible user actually
        changed. Mirrors MotionPerception.reset_dedup — called by
        SensingService on presence.enter via the orchestrator. Without this
        guard, every stranger flicker would wipe the key and bypass the
        5-min window.
        """
        with self._state_lock:
            if self._last_sent_key is None:
                return
            last_user = self._last_sent_key[0]
            if last_user == new_user:
                logger.debug(
                    "[activity.emotion] dedup reset skipped — same user %r",
                    last_user,
                )
                return
            logger.info(
                "[activity.emotion] dedup reset (user %r → %r, %d keys cleared)",
                last_user,
                new_user,
                len(self._last_sent_by_key),
            )
            self._last_sent_by_key.clear()
            self._last_sent_key = None
            self._last_sent_ts = 0.0

    def to_dict(self) -> dict[str, Any]:
        with self._state_lock:
            seconds_since = (
                int(time.time() - self._last_detection_time)
                if self._last_detection_time is not None
                else None
            )
            last_sent = self._last_sent_key
            return {
                "type": "emotion",
                "last_sent_emotion": last_sent[1] if last_sent else None,
                "last_sent_user": last_sent[0] if last_sent else None,
                "last_detected_emotion": self._last_emotion,
                "buffered_persons": len(self._emotion_buffer),
                "dedup_keys": len(self._last_sent_by_key),
                "emotion_detected": self._last_detection_time is not None,
                "seconds_since_detection": seconds_since,
            }
