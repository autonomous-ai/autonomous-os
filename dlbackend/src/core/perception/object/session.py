"""Per-connection object detection session."""

import asyncio
import time
from typing import Any

import cv2.typing as cv2t
from typing_extensions import override

from core.models.object import (
    ObjectDetection,
    ObjectDetectionItem,
    ObjectPerceptionSessionConfig,
    RawObjectDetection,
)
from core.perception.base import PerceptionSessionBase
from core.perception.object.predictors.base import ObjectDetector
from core.types import Omit, omit
from core.utils.common import get_or_default


class ObjectPerceptionSession(
    PerceptionSessionBase[
        cv2t.MatLike,
        ObjectDetection,
        ObjectPerceptionSessionConfig,
    ]
):
    DEFAULT_CONFIG: ObjectPerceptionSessionConfig = ObjectPerceptionSessionConfig()

    def __init__(
        self,
        object_detector: ObjectDetector,
        config: ObjectPerceptionSessionConfig | None = None,
    ) -> None:
        config = get_or_default(config, ObjectPerceptionSessionConfig())
        super().__init__(config)

        self._object_detector: ObjectDetector = object_detector
        self._running: bool = False

    @override
    async def start(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return
        self._running = True

    @override
    async def stop(self) -> None:
        self._running = False

    @override
    def is_ready(self) -> bool:
        if not self._object_detector.is_ready():
            return False
        return self._running

    @override
    async def update(self, input: cv2t.MatLike) -> ObjectDetection | None:
        """Run object detection on a single frame.

        Returns ObjectDetection with detected objects, or None if rate-limited.
        """
        cur_ts: float = time.time()
        if cur_ts - self._last_update_ts < self._config.frame_interval:
            return self._last_prediction

        H, W = input.shape[:2]

        raw_results: list[RawObjectDetection] = await asyncio.to_thread(
            self._object_detector.predict, [input], classes=self._config.classes,
        )
        raw: RawObjectDetection = raw_results[0]

        # Filter by threshold, rescale [0,1] → pixel xywh
        detections: list[ObjectDetectionItem] = []
        for i in range(len(raw.class_names)):
            if raw.confidence[i] >= self._config.threshold:
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

        result: ObjectDetection = ObjectDetection(detections=detections)

        self._last_update_ts = cur_ts
        self._last_prediction = result

        if detections:
            self._logger.info(
                "[session %s] Detected %d objects: %s",
                self._session_id,
                len(detections),
                ", ".join(f"{d.class_name} ({d.confidence:.2f})" for d in detections[:5]),
            )

        return result

    @override
    def update_config(
        self,
        *,
        frame_interval: float | Omit = omit,
        classes: list[str] | None | Omit = omit,
        threshold: float | Omit = omit,
        **kwargs: Any,
    ) -> None:
        super().update_config(
            frame_interval=frame_interval,
            classes=classes,
            threshold=threshold,
        )

    @override
    def _post_config_update(self) -> None:
        self._logger.info(
            "[session %s] Config updated — frame_interval=%.2f, threshold=%.2f, classes=%s",
            self._session_id,
            self._config.frame_interval,
            self._config.threshold,
            len(self._config.classes) if self._config.classes else "default",
        )
