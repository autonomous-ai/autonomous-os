"""Object detection pipeline: model lifecycle and session management."""

import asyncio

import cv2.typing as cv2t
from typing_extensions import override

from core.models.object import (
    ObjectDetection,
    ObjectDetectionItem,
    ObjectPerceptionSessionConfig,
    RawObjectDetection,
)
from core.perception.base import PerceptionBase
from core.perception.object.predictors.base import ObjectDetector
from core.perception.object.session import ObjectPerceptionSession
from core.perception.object.utils import ObjectDetectorFactory


class ObjectPerception(PerceptionBase[ObjectPerceptionSession]):
    """Object detection pipeline for a single detector. Loaded once, shared by all WS sessions."""

    def __init__(
        self,
        object_detector_factory: ObjectDetectorFactory,
        default_config: ObjectPerceptionSessionConfig | None = None,
    ) -> None:
        super().__init__()

        self._object_detector_factory: ObjectDetectorFactory = object_detector_factory
        self._default_config: ObjectPerceptionSessionConfig | None = default_config

        self._object_detector: ObjectDetector | None = None
        self._running: bool = False

    @override
    async def start(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return

        self._object_detector = self._object_detector_factory.create()
        await asyncio.to_thread(self._object_detector.start)

        self._running = True
        self._logger.info("Ready")

    @override
    async def stop(self) -> None:
        if self._object_detector is not None:
            await asyncio.to_thread(self._object_detector.stop)
            self._object_detector = None

        self._running = False
        self._logger.info("Stopped")

    @override
    def is_ready(self) -> bool:
        if not self._running or self._object_detector is None:
            return False
        return self._object_detector.is_ready()

    @override
    async def create_session(self) -> ObjectPerceptionSession:
        if self._object_detector is None:
            raise RuntimeError("ObjectPerception not started")

        config = self._default_config or ObjectPerceptionSession.DEFAULT_CONFIG
        return ObjectPerceptionSession(
            object_detector=self._object_detector,
            config=config,
        )

    # --- Single-shot prediction (for HTTP endpoints) ---

    async def predict_image(
        self,
        image: cv2t.MatLike,
        classes: list[str] | None = None,
    ) -> ObjectDetection:
        """Detect objects in a single image."""
        if self._object_detector is None:
            raise RuntimeError("ObjectPerception not started")

        H, W = image.shape[:2]

        raw_results: list[RawObjectDetection] = await asyncio.to_thread(
            self._object_detector.predict, [image], classes=classes
        )
        raw: RawObjectDetection = raw_results[0]

        # Rescale [0,1] → pixel xywh
        detections: list[ObjectDetectionItem] = []
        for i in range(len(raw.class_names)):
            xywh = raw.bbox_xywh[i].copy()
            xywh[0] *= W
            xywh[1] *= H
            xywh[2] *= W
            xywh[3] *= H
            detections.append(ObjectDetectionItem(
                class_name=raw.class_names[i],
                xywh=xywh.tolist(),
                confidence=float(raw.confidence[i]),
            ))

        return ObjectDetection(detections=detections)
