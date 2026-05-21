"""Base emotion predictor — classifies emotion from face crops.

Takes a batch of face crops, preprocesses, runs ONNX inference, and
returns raw expression probability distributions as numpy arrays.

Face detection is NOT done here — the session detects faces and passes
crops to this predictor (same pattern as action: session handles person
detection, predictor handles classification).

Concrete subclasses (EmoNet, PosterV2) override class-level defaults
(model path, input size, mean/std, classes file).
"""

from pathlib import Path
from typing import Any, cast

import cv2
import cv2.typing as cv2t
import numpy as np
import numpy.typing as npt
import onnxruntime as ort
from typing_extensions import override

from core.models.emotion import RawEmotionDetection
from core.perception.base import PredictorBase
from core.perception.emotion.constants import RESOURCES_DIR
from core.utils.common import get_or_default
from core.utils.compute import softmax
from core.utils.runtime import prepare_ort_session


class EmotionRecognizer(PredictorBase[cv2t.MatLike, RawEmotionDetection]):
    """Base class for emotion classifiers operating on face crops.

    Subclasses override class-level defaults. The base handles ONNX
    lifecycle, preprocessing, and inference. Class names are loaded
    from a text file at start time (same pattern as action recognizer).
    """

    DEFAULT_MODEL_PATH: Path | None = None
    DEFAULT_CLASSES_PATH: Path = RESOURCES_DIR / "posterv2_classes.txt"
    DEFAULT_INPUT_SIZE: tuple[int, int] = (224, 224)

    MEAN: npt.NDArray[np.float32] = np.array([0, 0, 0], dtype=np.float32)
    STD: npt.NDArray[np.float32] = np.array([1, 1, 1], dtype=np.float32)

    def __init__(
        self,
        model_path: Path | None = None,
        classes_path: Path | None = None,
        input_size: tuple[int, int] | None = None,
    ) -> None:
        super().__init__()

        model_path = get_or_default(model_path, self.DEFAULT_MODEL_PATH)
        if model_path is None:
            raise RuntimeError("model_path must not be None")

        self._model_path: Path = model_path
        self._classes_path: Path = get_or_default(classes_path, self.DEFAULT_CLASSES_PATH)
        self._input_size: tuple[int, int] = get_or_default(input_size, self.DEFAULT_INPUT_SIZE)

        self._class_names: list[str] = []
        self._running: bool = False
        self._session: ort.InferenceSession | None = None

    @property
    def class_names(self) -> list[str]:
        return self._class_names

    @property
    def input_size(self) -> tuple[int, int]:
        return self._input_size

    @override
    def _start_impl(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return

        self._logger.info("Loading model from %s", self._model_path)
        self._session = prepare_ort_session(self._model_path)
        self._class_names = self._load_classes(self._classes_path)
        self._running = True
        self._logger.info("Ready — %d emotion classes", len(self._class_names))

    @override
    def _stop_impl(self) -> None:
        self._session = None
        self._running = False
        self._logger.info("Stopped")

    @override
    def _is_ready_impl(self) -> bool:
        return self._running and self._session is not None

    @override
    def preprocess(self, input: list[cv2t.MatLike]) -> list[npt.NDArray[np.float32]]:
        """Default preprocessing: resize, BGR→RGB, normalize, CHW, add batch dim."""
        H, W = self._input_size
        results: list[npt.NDArray[np.float32]] = []
        for face_crop in input:
            resized: cv2t.MatLike = cv2.resize(face_crop, (W, H))
            rgb: cv2t.MatLike = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
            tensor: npt.NDArray[np.float32] = rgb.astype(np.float32) / 255.0
            tensor = (tensor - self.MEAN) / self.STD
            tensor = tensor.transpose(2, 0, 1)  # HWC → CHW
            results.append(tensor.astype(np.float32))  # (C, H, W)
        return results

    @override
    def _predict_impl(
        self,
        input: list[cv2t.MatLike],
        *,
        preprocess: bool = True,
        **kwargs: Any,
    ) -> list[RawEmotionDetection]:
        """Classify emotion for a batch of face crops.

        Stacks all crops into a single (N, C, H, W) tensor and runs
        ONNX inference in one pass. Returns one RawEmotionDetection per crop.

        Args:
            input: List of face crops (BGR).
            preprocess: If True, run preprocess on each crop. Set to False
                when input is already preprocessed.
        """
        preprocessed: list[npt.NDArray[np.float32]] = (
            self.preprocess(input) if preprocess else input
        )

        # Stack into single batch: (N, C, H, W)
        batch: npt.NDArray[np.float32] = np.stack(preprocessed, axis=0)
        raw_outputs: list[npt.NDArray] = self._session.run(None, {"input": batch})
        return self._postprocess_batch(raw_outputs, len(input))

    def _postprocess_batch(
        self, raw_outputs: list[npt.NDArray], N: int
    ) -> list[RawEmotionDetection]:
        """Convert batched ONNX output to per-sample RawEmotionDetection.

        Default: first output is expression logits (N, C). Subclasses
        override for models with additional outputs (valence, arousal).
        """
        logits: npt.NDArray[np.float32] = cast(npt.NDArray[np.float32], raw_outputs[0])
        probs: npt.NDArray[np.float32] = softmax(logits, axis=-1)  # (N, C)

        return [
            RawEmotionDetection(expression_probs=probs[i])
            for i in range(N)
        ]

    @staticmethod
    def _load_classes(classes_path: Path) -> list[str]:
        return classes_path.read_text().strip().split("\n")
