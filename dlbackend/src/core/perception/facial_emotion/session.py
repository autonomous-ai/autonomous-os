"""Per-connection emotion detection session.

Handles face detection (via YuNet), passes crops to the EmotionRecognizer,
filters results by threshold, and manages rate limiting.
"""

import asyncio
import time
from typing import Any

import cv2.typing as cv2t
import numpy as np
from typing_extensions import override

from core.models.face import FaceCrop
from core.models.facial_emotion import (
    Emotion,
    EmotionDetection,
    EmotionPerceptionSessionConfig,
    RawEmotionDetection,
)
from core.perception.base import PerceptionSessionBase
from core.perception.face.predictors.base import FaceDetector
from core.perception.facial_emotion.predictors.base import EmotionRecognizer
from core.types import Omit, omit
from core.utils.common import get_or_default


class EmotionPerceptionSession(
    PerceptionSessionBase[
        cv2t.MatLike,
        EmotionDetection,
        EmotionPerceptionSessionConfig,
    ]
):
    """Per-connection session for emotion detection."""

    DEFAULT_CONFIG: EmotionPerceptionSessionConfig = EmotionPerceptionSessionConfig()

    def __init__(
        self,
        emotion_recognizer: EmotionRecognizer,
        face_detector: FaceDetector,
        config: EmotionPerceptionSessionConfig | None = None,
    ) -> None:
        config = get_or_default(config, EmotionPerceptionSessionConfig())
        super().__init__(config)
        self._emotion_recognizer: EmotionRecognizer = emotion_recognizer
        self._face_detector: FaceDetector = face_detector
        self._running: bool = False

    @override
    async def start(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return

        if not self._emotion_recognizer.is_ready():
            await asyncio.to_thread(self._emotion_recognizer.start)

        if not self._face_detector.is_ready():
            await asyncio.to_thread(self._face_detector.start)

        self._running = True

    @override
    async def stop(self) -> None:
        self._running = False

    @override
    def is_ready(self) -> bool:
        return (
            self._running and self._emotion_recognizer.is_ready() and self._face_detector.is_ready()
        )

    @override
    async def update(self, input: cv2t.MatLike) -> EmotionDetection | None:
        """Detect faces, classify emotions, filter by threshold."""
        cur_ts: float = time.time()
        if cur_ts - self._last_update_ts < self._config.frame_interval:
            return self._last_prediction

        # Detect faces and extract crops
        face_crops_per_frame: list[list[FaceCrop]] = await asyncio.to_thread(
            self._face_detector.extract_crops, [input]
        )
        face_crops: list[FaceCrop] = face_crops_per_frame[0] if face_crops_per_frame else []

        if not face_crops:
            self._last_prediction: EmotionDetection = EmotionDetection(emotions=[])
            self._last_update_ts: float = cur_ts
            return self._last_prediction

        # Classify emotions on each face crop
        crops: list[cv2t.MatLike] = [fc.crop for fc in face_crops]
        raw_detections: list[RawEmotionDetection] = await asyncio.to_thread(
            self._emotion_recognizer.predict, crops
        )

        # Combine emotion_recognizer output with face detector info, filter by threshold
        emotions: list[Emotion] = []
        for face_crop, raw in zip(face_crops, raw_detections):
            emotion_idx: int = int(np.argmax(raw.expression_probs))
            confidence: float = float(raw.expression_probs[emotion_idx])

            if confidence < self._config.confidence_threshold:
                continue

            emotions.append(
                Emotion(
                    emotion=self._emotion_recognizer.class_names[emotion_idx],
                    confidence=confidence,
                    face_confidence=face_crop.confidence,
                    bbox=face_crop.bbox_xyxy,
                    valence=raw.valence,
                    arousal=raw.arousal,
                )
            )

        self._last_prediction = EmotionDetection(emotions=emotions)
        self._last_update_ts = cur_ts

        if emotions:
            self._logger.info(
                "[session %s] Detected %d face(s): %s",
                self._session_id,
                len(emotions),
                ", ".join(f"{e.emotion} ({e.confidence:.2f})" for e in emotions),
            )

        return self._last_prediction

    @override
    def update_config(
        self,
        *,
        confidence_threshold: float | Omit = omit,
        frame_interval: float | Omit = omit,
        **kwargs: Any,
    ) -> None:
        super().update_config(
            confidence_threshold=confidence_threshold,
            frame_interval=frame_interval,
        )

    @override
    def _post_config_update(self) -> None:
        self._logger.info(
            "[session %s] Config updated — threshold=%.2f, frame_interval=%.2f",
            self._session_id,
            self._config.confidence_threshold,
            self._config.frame_interval,
        )
