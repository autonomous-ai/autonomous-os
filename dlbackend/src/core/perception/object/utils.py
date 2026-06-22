"""Factory functions and factory classes for object detectors."""

from pathlib import Path

from core.enums.object import ObjectDetectorEnum
from core.perception.base import PredictorFactory
from core.perception.object.predictors.base import ObjectDetector


class ObjectDetectorFactory(PredictorFactory[ObjectDetector]):
    """Factory that creates ObjectDetector instances from config."""

    def __init__(
        self,
        model_name: ObjectDetectorEnum,
        model_path: Path | None = None,
        remote_url: str | None = None,
        classes_path: Path | None = None,
        threshold: float | None = None,
        batch_size: int | None = None,
    ) -> None:
        self._model_name = model_name
        self._model_path = model_path
        self._remote_url = remote_url
        self._classes_path = classes_path
        self._threshold = threshold
        self._batch_size = batch_size

    def create(self) -> ObjectDetector:
        return create_object_detector(
            self._model_name,
            model_path=self._model_path,
            remote_url=self._remote_url,
            classes_path=self._classes_path,
            threshold=self._threshold,
            batch_size=self._batch_size,
        )


def create_object_detector(
    model_name: ObjectDetectorEnum,
    model_path: Path | None = None,
    remote_url: str | None = None,
    classes_path: Path | None = None,
    threshold: float | None = None,
    batch_size: int | None = None,
) -> ObjectDetector:
    """Instantiate the correct object detector.

    Uses ONNX predictors when an ONNX model path is provided (ends with .onnx),
    otherwise falls back to PyTorch/HuggingFace predictors.
    """
    use_onnx = model_path is not None and str(model_path).endswith(".onnx")

    if model_name == ObjectDetectorEnum.YOLO_WORLD:
        if use_onnx:
            from core.perception.object.predictors.onnx.yolo_world import (
                YOLOWorldONNXDetector as detector_cls,
            )
        else:
            from core.perception.object.predictors.torch.yolo_world import (
                YOLOWorldDetector as detector_cls,
            )
    elif model_name == ObjectDetectorEnum.OWLV2:
        if use_onnx:
            from core.perception.object.predictors.onnx.owlv2 import (
                OWLv2ONNXDetector as detector_cls,
            )
        else:
            from core.perception.object.predictors.torch.owlv2 import OWLv2Detector as detector_cls
    elif model_name == ObjectDetectorEnum.GROUNDING_DINO:
        if use_onnx:
            from core.perception.object.predictors.onnx.grounding_dino import (
                GroundingDINOONNXDetector as detector_cls,
            )
        else:
            from core.perception.object.predictors.torch.grounding_dino import (
                GroundingDINODetector as detector_cls,
            )
    else:
        raise ValueError(f"Unknown object detector: {model_name}")

    return detector_cls(
        model_path=model_path, remote_url=remote_url, classes_path=classes_path,
        threshold=threshold, batch_size=batch_size,
    )
