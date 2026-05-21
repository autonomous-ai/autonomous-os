"""YOLO-based person detector for action recognition preprocessing.

Detects person bounding boxes in a frame and exposes ``detect_largest_crop``
to extract the crop of the largest person for downstream action recognition.
"""

from typing import Any

import cv2.typing as cv2t
import numpy as np
from typing_extensions import override
from ultralytics.models.yolo import YOLO

from core.models.person import RawPersonDetection
from core.perception.person.predictors.base import PersonDetector
from core.utils.common import get_or_default

# COCO class index for "person"
_PERSON_CLASS_ID = 0


class YOLOPersonDetector(PersonDetector):
    """YOLO-based person detector.

    Loads an ultralytics YOLO model once and runs inference to locate people
    in BGR frames.  Rate-limiting is handled by the caller, so this class
    runs on every frame it receives.

    Usage::

        detector = YOLOPersonDetector(model_name="yolo12x.pt")
        detector.start()
        crop = detector.detect_largest_crop(frame)   # ndarray or None
    """

    DEFAULT_MODEL_NAME: str = "yolo12x.pt"
    DEFAULT_CONFIDENCE_THRESHOLD: float = 0.4
    DEFAULT_BBOX_EXPAND_SCALE: float = 2.0

    DEFAULT_MIN_AREA_RATIO: float = 0.25

    def __init__(
        self,
        model_path: str | None = None,
        threshold: float | None = None,
        bbox_expand_scale: float | None = None,
    ):
        super().__init__()
        self._model_name: str = get_or_default(model_path, self.DEFAULT_MODEL_NAME)
        self._threshold: float = get_or_default(threshold, self.DEFAULT_CONFIDENCE_THRESHOLD)
        self._bbox_expand_scale: float = get_or_default(
            bbox_expand_scale, self.DEFAULT_BBOX_EXPAND_SCALE
        )

        self._model: YOLO | None = None
        self._running: bool = False

    @override
    def _start_impl(self) -> None:
        """Load the YOLO model weights (blocking)."""
        if self._running:
            self._logger.info("Already running")

        self._model = YOLO(self._model_name)
        self._running = True

    @override
    def _stop_impl(self) -> None:
        self._model = None
        self._running = False

    @override
    def _is_ready_impl(self) -> bool:
        return self._running and self._model is not None

    def _scale_and_clamp_bbox(self, bbox: list[int], h: int, w: int, scale: float = 1.0):
        x1, y1, x2, y2 = bbox
        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2

        x1 = int(max(min(cx + (x1 - cx) * scale, w - 1), 0))
        x2 = int(max(min(cx + (x2 - cx) * scale, w - 1), 0))
        y1 = int(max(min(cy + (y1 - cy) * scale, h - 1), 0))
        y2 = int(max(min(cy + (y2 - cy) * scale, h - 1), 0))

        return [x1, y1, x2, y2]

    @override
    def _predict_impl(self, input: list[cv2t.MatLike], *, preprocess: bool = True, **kwargs: Any) -> list[RawPersonDetection]:
        """Run person detection on each frame and return all person detections."""
        _EMPTY: RawPersonDetection = RawPersonDetection(
            bbox_xyxy=np.zeros((0, 4), dtype=np.float32),
            confidence=np.zeros(0, dtype=np.float32),
        )

        person_detections: list[RawPersonDetection] = []
        for frame in input:
            try:
                H, W = frame.shape[:2]
                results = self._model(
                    frame,
                    classes=[_PERSON_CLASS_ID],
                    conf=self._threshold,
                    verbose=False,
                )
                bbox_xyxy_list: list[list[int]] = []
                conf_list: list[float] = []
                for r in results:
                    if r.boxes is None or len(r.boxes) == 0:
                        continue

                    for box in r.boxes:
                        raw: list[int] = [int(v) for v in box.xyxy[0].tolist()]
                        x1, y1, x2, y2 = self._scale_and_clamp_bbox(
                            raw, H, W, self._bbox_expand_scale
                        )

                        bbox_xyxy_list.append([x1, y1, x2, y2])
                        conf_list.append(float(box.conf[0]))

                if not bbox_xyxy_list:
                    person_detections.append(_EMPTY)
                else:
                    person_detections.append(
                        RawPersonDetection(
                            bbox_xyxy=np.array(bbox_xyxy_list, dtype=np.float32),
                            confidence=np.array(conf_list, dtype=np.float32),
                        )
                    )
            except Exception:
                self._logger.exception("Inference error")
                person_detections.append(_EMPTY)

        return person_detections

    @override
    def preprocess(self, input: list[cv2t.MatLike]) -> list[cv2t.MatLike]:
        """No preprocessing needed — YOLO handles internally."""
        return input
