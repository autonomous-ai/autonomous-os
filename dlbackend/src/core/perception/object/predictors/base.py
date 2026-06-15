"""Abstract base class for zero-shot object detectors.

Extends PredictorBase. Input is MatLike (BGR image), output is RawObjectDetection.
Classes to detect are passed via the `classes` kwarg on predict().
"""

from abc import ABC
from pathlib import Path
from typing import Any

import cv2.typing as cv2t
from typing_extensions import override

from core.models.object import RawObjectDetection
from core.perception.base import PredictorBase
from core.perception.object.constants import RESOURCES_DIR
from core.utils.common import get_or_default


class ObjectDetector(PredictorBase[cv2t.MatLike, RawObjectDetection], ABC):
    """Base interface for zero-shot object detectors.

    Subclasses implement _predict_impl with classes passed via kwargs.
    The public predict() adds a typed `classes` parameter.
    """

    DEFAULT_MODEL_PATH: Path | None = None
    DEFAULT_CLASSES_PATH: Path = RESOURCES_DIR / "default_classes.txt"
    DEFAULT_THRESHOLD: float = 0.25

    def __init__(
        self,
        model_path: Path | None = None,
        classes_path: Path | None = None,
        threshold: float | None = None,
        batch_size: int | None = None,
    ) -> None:
        super().__init__(batch_size=batch_size)

        model_path = get_or_default(model_path, self.DEFAULT_MODEL_PATH)
        if model_path is None:
            raise RuntimeError("model_path must not be None")

        self._model_path: Path = model_path
        self._classes_path: Path = get_or_default(classes_path, self.DEFAULT_CLASSES_PATH)
        self._threshold: float = get_or_default(threshold, self.DEFAULT_THRESHOLD)

        # Populated in _start_impl via _load_classes()
        self._class_names: list[str] = []

    @property
    def class_names(self) -> list[str]:
        return self._class_names

    def _load_classes(self, classes_path: Path) -> list[str]:
        return classes_path.read_text().strip().split("\n")

    @override
    def predict(
        self,
        input: list[cv2t.MatLike],
        *,
        preprocess: bool = True,
        classes: list[str] | None = None,
        **kwargs: Any,
    ) -> list[RawObjectDetection]:
        with self._gpu_lock:
            return super().predict(input, preprocess=preprocess, classes=classes, **kwargs)
