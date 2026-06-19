"""Abstract base classes for object detectors.

ObjectDetector — base for all detectors (PyTorch, HF, ONNX).
ONNXObjectDetector — base for ONNX-based detectors with shared lifecycle,
    postprocessing (padding filter, threshold, NMS), and batched inference.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, cast

import cv2.typing as cv2t
import numpy as np
import numpy.typing as npt
import onnxruntime as ort
from typing_extensions import override

from core.models.object import RawObjectDetection
from core.perception.base import PredictorBase
from core.perception.object.constants import RESOURCES_DIR
from core.utils.common import get_or_default
from core.utils.detection import nms_xywh
from core.utils.files import ensure_downloaded
from core.utils.runtime import prepare_ort_session


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
        return super().predict(input, preprocess=preprocess, classes=classes, **kwargs)


class ONNXObjectDetector(ObjectDetector, ABC):
    """Base for ONNX-based object detectors.

    Provides shared ONNX lifecycle (_start_impl, _stop_impl, _is_ready_impl),
    batched inference (_predict_impl), and postprocessing (padding filter,
    threshold, NMS). Subclasses configure I/O names, preprocessing, and
    tokenization.

    Output boxes are normalized [0, 1]. The consumer applies model-specific
    unscaling (unletterbox_boxes, unowlv2_boxes, etc.).
    """

    DEFAULT_REMOTE_URL: str | None = None

    ONNX_INPUT_NAMES: list[str] = ["images"]
    ONNX_OUTPUT_NAMES: list[str] = ["boxes", "probs", "labels"]

    def __init__(
        self,
        model_path: Path | None = None,
        remote_url: str | None = None,
        classes_path: Path | None = None,
        threshold: float | None = None,
        batch_size: int | None = None,
        nms: bool = True,
    ) -> None:
        super().__init__(
            model_path=model_path,
            classes_path=classes_path,
            threshold=threshold,
            batch_size=batch_size,
        )
        self._remote_url: str | None = get_or_default(remote_url, self.DEFAULT_REMOTE_URL)
        self._nms: bool = nms
        self._session: ort.InferenceSession | None = None
        self._running: bool = False

    @override
    def _start_impl(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return

        self._model_path = ensure_downloaded(self._model_path, remote=self._remote_url)
        self._logger.info("Loading ONNX model from %s", self._model_path)
        self._session = prepare_ort_session(self._model_path)
        self._start_tokenizer()
        self._class_names = self._load_classes(self._classes_path)
        self._running = True
        self._logger.info(
            "Ready — %d default classes, nms=%s", len(self._class_names), self._nms
        )

    def _start_tokenizer(self) -> None:
        """Load any tokenizer/processor needed for text encoding.

        Override in subclasses that need a tokenizer (zero-shot detectors).
        No-op by default (e.g. YOLO person detector has no text input).
        """

    @override
    def _stop_impl(self) -> None:
        self._session = None
        self._running = False
        self._logger.info("Stopped")

    @override
    def _is_ready_impl(self) -> bool:
        return self._running and self._session is not None

    @abstractmethod
    def _build_onnx_inputs(
        self,
        img_batch: npt.NDArray[np.float32],
        classes: list[str],
    ) -> dict[str, npt.NDArray]:
        """Build the ONNX session input dict from preprocessed images + classes.

        Subclasses tokenize text queries and combine with images here.
        For single-input models (YOLO person), just return {"images": img_batch}.
        """

    @abstractmethod
    def _to_orig_normalized(
        self,
        boxes_xywh: npt.NDArray[np.float32],
        orig_hw: tuple[int, int],
    ) -> npt.NDArray[np.float32]:
        """Convert model-space normalized [0,1] xywh to original-image normalized [0,1] xywh.

        Subclasses undo their specific preprocessing transform (letterbox,
        owl2 padding, etc.) and re-normalize to the original image dims.
        """

    @override
    def _predict_impl(
        self,
        input: list[cv2t.MatLike],
        *,
        preprocess: bool = True,
        classes: list[str] | None = None,
        **kwargs: Any,
    ) -> list[RawObjectDetection]:
        if self._session is None:
            raise RuntimeError(f"{self.__class__.__name__} is not ready")

        effective_classes: list[str] = classes if classes else self._class_names

        # Capture original sizes before preprocessing
        orig_hws: list[tuple[int, int]] = [(img.shape[0], img.shape[1]) for img in input]

        if preprocess:
            input = self.preprocess(input)

        img_batch: npt.NDArray[np.float32] = np.stack(input, axis=0)

        onnx_inputs = self._build_onnx_inputs(img_batch, effective_classes)
        with self._gpu_lock:
            raw_outputs = self._session.run(self.ONNX_OUTPUT_NAMES, onnx_inputs)

        return self._postprocess_batch(raw_outputs, effective_classes, orig_hws)

    def _postprocess_batch(
        self,
        raw_outputs: list[npt.NDArray],
        classes: list[str],
        orig_hws: list[tuple[int, int]],
    ) -> list[RawObjectDetection]:
        """Convert batched ONNX outputs to per-image RawObjectDetection.

        Shared across all ONNX object detectors: filters padding, applies
        threshold, optionally runs NMS. Boxes are returned as xywh normalized
        [0, 1] relative to the original image dimensions.
        """
        all_boxes = cast(npt.NDArray[np.float32], raw_outputs[0])   # [B, N, 4]
        all_probs = cast(npt.NDArray[np.float32], raw_outputs[1])   # [B, N, K]
        all_labels = cast(npt.NDArray[np.int64], raw_outputs[2])    # [B, N]

        results: list[RawObjectDetection] = []
        for i in range(all_boxes.shape[0]):
            boxes = all_boxes[i]
            probs = all_probs[i]
            labels = all_labels[i]

            # Filter padding (-1) and invalid labels
            valid = (labels >= 0) & (labels < len(classes))
            boxes = boxes[valid]
            probs = probs[valid]
            labels = labels[valid]

            if len(boxes) == 0:
                results.append(RawObjectDetection(
                    bbox_xywh=np.zeros((0, 4), dtype=np.float32),
                    class_names=[],
                    confidence=np.zeros(0, dtype=np.float32),
                ))
                continue

            # Per-detection confidence = score of the assigned class
            conf = np.array(
                [probs[j, labels[j]] for j in range(len(labels))],
                dtype=np.float32,
            )

            # Filter by threshold
            keep = conf >= self._threshold
            boxes = boxes[keep]
            labels = labels[keep]
            conf = conf[keep]

            # Runtime NMS
            if self._nms and len(boxes) > 0:
                keep_nms = nms_xywh(boxes, conf)
                boxes = boxes[keep_nms]
                labels = labels[keep_nms]
                conf = conf[keep_nms]

            names = [classes[int(idx)] for idx in labels]

            # Convert from model-space normalized [0,1] to original-image
            # normalized [0,1] via pixel coords as intermediate.
            orig_hw = orig_hws[i]
            norm_xywh = self._to_orig_normalized(boxes.astype(np.float32), orig_hw)

            results.append(RawObjectDetection(
                bbox_xywh=norm_xywh,
                class_names=names,
                confidence=conf,
            ))

        return results
