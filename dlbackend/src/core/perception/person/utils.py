from core.enums.person import PersonDetectorEnum
from core.perception.base import PredictorFactory
from core.perception.person.predictors.base import PersonDetector


class PersonDetectorFactory(PredictorFactory[PersonDetector]):
    """Factory that creates PersonDetector instances from config."""

    def __init__(
        self,
        model_name: PersonDetectorEnum,
        model_path: str | None = None,
        threshold: float | None = None,
        bbox_expand_scale: float | None = None,
    ) -> None:
        self._model_name = model_name
        self._model_path = model_path
        self._threshold = threshold
        self._bbox_expand_scale = bbox_expand_scale

    def create(self) -> PersonDetector:
        return create_person_detector(
            self._model_name, self._model_path,
            threshold=self._threshold, bbox_expand_scale=self._bbox_expand_scale,
        )


def create_person_detector(
    model_name: PersonDetectorEnum,
    model_path: str | None = None,
    threshold: float | None = None,
    bbox_expand_scale: float | None = None,
) -> PersonDetector:
    """Instantiate the correct recognizer model."""
    if model_name == PersonDetectorEnum.YOLO:
        from core.perception.person.predictors.yolo import YOLOPersonDetector as predictor_cls
    else:
        msg = f"Unknown person detector model: {model_name}"
        raise ValueError(msg)

    return predictor_cls(model_path, threshold=threshold, bbox_expand_scale=bbox_expand_scale)
