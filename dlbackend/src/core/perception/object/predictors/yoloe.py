"""YOLO-E zero-shot object detector."""

from pathlib import Path
from typing import Any

import cv2.typing as cv2t
import numpy as np
import numpy.typing as npt
import torch
from typing_extensions import override
from ultralytics import YOLOE

from core.models.object import RawObjectDetection

from .base import ObjectDetector


class YOLOEDetector(ObjectDetector):
    """Zero-shot object detection using YOLO-E (ultralytics).

    Classes are set per-request via model.set_classes().
    """

    DEFAULT_MODEL_PATH: Path = Path("yoloe-26x-seg.pt")

    def __init__(
        self,
        model_path: Path | None = None,
        classes_path: Path | None = None,
        threshold: float | None = None,
    ) -> None:
        super().__init__(model_path=model_path, classes_path=classes_path, threshold=threshold)
        self._model: YOLOE | None = None
        self._running: bool = False

    @override
    def _start_impl(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return

        self._logger.info("Loading model from %s", self._model_path)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        self._model = YOLOE(str(self._model_path)).to(device)
        self._class_names = self._load_classes(self._classes_path)
        self._running = True
        self._logger.info("Ready")

    @override
    def _stop_impl(self) -> None:
        self._model = None
        self._running = False
        self._logger.info("Stopped")

    @override
    def _is_ready_impl(self) -> bool:
        return self._running and self._model is not None

    @override
    def preprocess(self, input: list[cv2t.MatLike]) -> list[cv2t.MatLike]:
        return input

    @override
    def _predict_impl(
        self,
        input: list[cv2t.MatLike],
        *,
        preprocess: bool = True,
        classes: list[str] | None = None,
        **kwargs: Any,
    ) -> list[RawObjectDetection]:
        if self._model is None:
            raise RuntimeError("Model not started")

        effective_classes: list[str] = classes if classes else self._class_names
        self._model.set_classes(effective_classes)

        results: list[RawObjectDetection] = []
        preds = self._model.predict(input, verbose=False, conf=self._threshold)

        for result in preds:
            boxes = result.boxes
            if boxes is None or len(boxes) == 0:
                results.append(RawObjectDetection(
                    bbox_xywh=np.zeros((0, 4), dtype=np.float32),
                    class_names=[],
                    confidence=np.zeros(0, dtype=np.float32),
                ))
                continue

            xyxy_np: npt.NDArray[np.float32] = boxes.xyxy.cpu().numpy().astype(np.float32)
            conf_np: npt.NDArray[np.float32] = boxes.conf.cpu().numpy().astype(np.float32)
            cls_np: npt.NDArray[np.int64] = boxes.cls.cpu().numpy().astype(np.int64)

            valid_np = cls_np < len(effective_classes)
            xyxy_np = xyxy_np[valid_np]
            conf_np = conf_np[valid_np]
            cls_np = cls_np[valid_np]

            xywh_np: npt.NDArray[np.float32] = np.empty_like(xyxy_np)
            xywh_np[:, 0] = (xyxy_np[:, 0] + xyxy_np[:, 2]) / 2
            xywh_np[:, 1] = (xyxy_np[:, 1] + xyxy_np[:, 3]) / 2
            xywh_np[:, 2] = xyxy_np[:, 2] - xyxy_np[:, 0]
            xywh_np[:, 3] = xyxy_np[:, 3] - xyxy_np[:, 1]

            names: list[str] = [effective_classes[i] for i in cls_np]

            results.append(RawObjectDetection(
                bbox_xywh=xywh_np,
                class_names=names,
                confidence=conf_np,
            ))

        return results
