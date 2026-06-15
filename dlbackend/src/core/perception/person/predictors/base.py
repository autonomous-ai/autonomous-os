"""Abstract base class for person detectors."""

import logging
from abc import ABC
from typing import Any

import cv2
import cv2.typing as cv2t

from core.models.person import RawPersonDetection
from core.perception.base import PredictorBase

logger = logging.getLogger(__name__)


class PersonDetector(PredictorBase[cv2t.MatLike, RawPersonDetection], ABC):
    """Base interface for person detectors.

    Subclasses implement ``start``, ``stop``, ``is_ready``, and ``predict``.
    ``extract_largest_crop`` is provided by the base class.
    """

    def predict(
        self,
        input: list[cv2t.MatLike],
        *,
        preprocess: bool = True,
        **kwargs: Any,
    ) -> list[RawPersonDetection]:
        with self._gpu_lock:
            return super().predict(input, preprocess=preprocess, **kwargs)

    def extract_largest_crop(
        self,
        input: list[cv2t.MatLike],
        min_area_ratio: float = 0.0,
    ) -> list[cv2.typing.MatLike | None]:
        """Return a crop of the largest detected person in each frame.

        Skips persons whose area is below ``min_area_ratio`` of the frame.
        Returns ``None`` per frame when no qualifying person is found.
        """
        detections: list[RawPersonDetection] = self.predict(input)

        cropped_input: list[cv2.typing.MatLike | None] = []
        for i, detected_people in enumerate(detections):
            if len(detected_people.bbox_xyxy) == 0:
                cropped_input.append(None)
                continue

            H, W = input[i].shape[:2]
            frame_area: int = H * W
            filter_mask = (detected_people.area / frame_area) > min_area_ratio

            if filter_mask.sum() == 0:
                cropped_input.append(None)
                continue

            largest_id: int = int(detected_people.area.argmax(0))

            x1, y1, x2, y2 = detected_people.bbox_xyxy[largest_id]

            x1, y1 = int(max(0, x1)), int(max(0, y1))
            x2, y2 = int(min(W, x2)), int(min(H, y2))

            if x1 >= x2 or y1 >= y2:
                cropped_input.append(None)
                continue

            cropped_input.append(input[i][y1:y2, x1:x2])

        return cropped_input
