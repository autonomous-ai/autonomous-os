"""Emotion perception: model lifecycle, session management, and single-shot prediction.

Wraps an EmotionRecognizer + FaceDetector.
Each WebSocket connection creates an EmotionPerceptionSession via create_session().
Single-shot methods (predict_face, predict_image) are provided for HTTP endpoints.
"""

import asyncio

import cv2.typing as cv2t
import numpy as np
import numpy.typing as npt
from typing_extensions import override

from core.models.emotion import (
    Emotion,
    EmotionDetection,
    EmotionPerceptionSessionConfig,
    RawEmotionDetection,
)
from core.models.face import FaceCrop
from core.perception.base import PerceptionBase
from core.perception.emotion.predictors.base import EmotionRecognizer
from core.perception.emotion.session import EmotionPerceptionSession
from core.perception.emotion.utils import EmotionRecognizerFactory
from core.perception.face.predictors.base import FaceDetector
from core.perception.face.utils import FaceDetectorFactory


class EmotionPerception(PerceptionBase[EmotionPerceptionSession]):
    """Emotion detection pipeline. Loaded once, shared by all WS sessions."""

    def __init__(
        self,
        emotion_recognizer_factory: EmotionRecognizerFactory,
        face_detector_factory: FaceDetectorFactory,
        default_config: EmotionPerceptionSessionConfig | None = None,
    ) -> None:
        super().__init__()

        self._emotion_recognizer_factory: EmotionRecognizerFactory = emotion_recognizer_factory
        self._face_detector_factory: FaceDetectorFactory = face_detector_factory
        self._default_config: EmotionPerceptionSessionConfig | None = default_config

        self._emotion_recognizer: EmotionRecognizer | None = None
        self._face_detector: FaceDetector | None = None
        self._running: bool = False

    @override
    async def start(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return

        self._emotion_recognizer = self._emotion_recognizer_factory.create()
        await asyncio.to_thread(self._emotion_recognizer.start)

        self._face_detector = self._face_detector_factory.create()
        await asyncio.to_thread(self._face_detector.start)

        self._running = True
        self._logger.info("Ready")

    @override
    async def stop(self) -> None:
        if self._emotion_recognizer is not None:
            await asyncio.to_thread(self._emotion_recognizer.stop)
            self._emotion_recognizer = None

        if self._face_detector is not None:
            await asyncio.to_thread(self._face_detector.stop)
            self._face_detector = None

        self._running = False
        self._logger.info("Stopped")

    @override
    def is_ready(self) -> bool:
        return (
            self._running
            and self._emotion_recognizer is not None
            and self._emotion_recognizer.is_ready()
            and self._face_detector is not None
            and self._face_detector.is_ready()
        )

    @override
    async def create_session(self) -> EmotionPerceptionSession:
        if self._emotion_recognizer is None or self._face_detector is None:
            raise RuntimeError("EmotionPerception not started")

        config: EmotionPerceptionSessionConfig = (
            self._default_config or EmotionPerceptionSession.DEFAULT_CONFIG
        )
        return EmotionPerceptionSession(
            emotion_recognizer=self._emotion_recognizer,
            face_detector=self._face_detector,
            config=config,
        )

    # --- Single-shot prediction (for HTTP endpoints) ---

    async def predict_face(self, face_crop: cv2t.MatLike) -> Emotion | None:
        """Classify emotion from a single pre-cropped face image."""
        if self._emotion_recognizer is None:
            raise RuntimeError("EmotionPerception not started")

        raw_results: list[RawEmotionDetection] = await asyncio.to_thread(
            self._emotion_recognizer.predict, [face_crop]
        )
        if not raw_results:
            return None

        raw: RawEmotionDetection = raw_results[0]
        emotion_idx: int = int(np.argmax(raw.expression_probs))
        H, W = face_crop.shape[:2]

        return Emotion(
            emotion=self._emotion_recognizer.class_names[emotion_idx],
            confidence=float(raw.expression_probs[emotion_idx]),
            face_confidence=1.0,
            bbox=[0, 0, W, H],
            valence=raw.valence,
            arousal=raw.arousal,
        )

    async def predict_image(self, frame: npt.NDArray[np.uint8]) -> EmotionDetection:
        """Detect faces in a full frame and classify emotion for each."""
        if self._face_detector is None or self._emotion_recognizer is None:
            raise RuntimeError("EmotionPerception not started")

        all_crops: list[list[FaceCrop]] = await asyncio.to_thread(
            self._face_detector.extract_crops, [frame]
        )
        face_crops: list[FaceCrop] = all_crops[0]
        if not face_crops:
            return EmotionDetection(emotions=[])

        crops: list[cv2t.MatLike] = [fc.crop for fc in face_crops]
        raw_results: list[RawEmotionDetection] = await asyncio.to_thread(
            self._emotion_recognizer.predict, crops
        )

        emotions: list[Emotion] = []
        for fc, raw in zip(face_crops, raw_results):
            emotion_idx: int = int(np.argmax(raw.expression_probs))
            emotions.append(
                Emotion(
                    emotion=self._emotion_recognizer.class_names[emotion_idx],
                    confidence=float(raw.expression_probs[emotion_idx]),
                    face_confidence=fc.confidence,
                    bbox=fc.bbox_xyxy,
                    valence=raw.valence,
                    arousal=raw.arousal,
                )
            )

        return EmotionDetection(emotions=emotions)
