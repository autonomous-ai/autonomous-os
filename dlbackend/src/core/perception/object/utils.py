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
        classes_path: Path | None = None,
        threshold: float | None = None,
    ) -> None:
        self._model_name = model_name
        self._model_path = model_path
        self._classes_path = classes_path
        self._threshold = threshold

    def create(self) -> ObjectDetector:
        return create_object_detector(
            self._model_name,
            model_path=self._model_path,
            classes_path=self._classes_path,
            threshold=self._threshold,
        )


def create_object_detector(
    model_name: ObjectDetectorEnum,
    model_path: Path | None = None,
    classes_path: Path | None = None,
    threshold: float | None = None,
) -> ObjectDetector:
    """Instantiate the correct object detector."""
    if model_name == ObjectDetectorEnum.YOLO_WORLD:
        from core.perception.object.predictors.yolo_world import YOLOWorldDetector as detector_cls
    elif model_name == ObjectDetectorEnum.YOLOE:
        from core.perception.object.predictors.yoloe import YOLOEDetector as detector_cls
    elif model_name == ObjectDetectorEnum.OWLV2:
        from core.perception.object.predictors.owlv2 import OWLv2Detector as detector_cls
    elif model_name == ObjectDetectorEnum.GROUNDING_DINO:
        from core.perception.object.predictors.grounding_dino import GroundingDINODetector as detector_cls
    else:
        raise ValueError(f"Unknown object detector: {model_name}")

    return detector_cls(model_path=model_path, classes_path=classes_path, threshold=threshold)
