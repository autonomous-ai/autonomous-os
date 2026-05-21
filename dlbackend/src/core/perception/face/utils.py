"""Factory functions and factory classes for face detectors."""

from pathlib import Path

from core.enums.face import FaceDetectorEnum
from core.perception.base import PredictorFactory
from core.perception.face.predictors.base import FaceDetector


class FaceDetectorFactory(PredictorFactory[FaceDetector]):
    """Factory that creates FaceDetector instances from config."""

    def __init__(
        self,
        model_name: FaceDetectorEnum,
        model_path: Path | None = None,
        score_threshold: float | None = None,
        nms_threshold: float | None = None,
    ) -> None:
        self._model_name = model_name
        self._model_path = model_path
        self._score_threshold = score_threshold
        self._nms_threshold = nms_threshold

    def create(self) -> FaceDetector:
        return create_face_detector(
            self._model_name, self._model_path,
            score_threshold=self._score_threshold,
            nms_threshold=self._nms_threshold,
        )


def create_face_detector(
    model_name: FaceDetectorEnum,
    model_path: Path | None = None,
    score_threshold: float | None = None,
    nms_threshold: float | None = None,
) -> FaceDetector:
    """Instantiate the correct face detector model."""
    if model_name == FaceDetectorEnum.YUNET:
        from core.perception.face.predictors.yunet import YuNetFaceDetector as detector_cls
    else:
        msg: str = f"Unknown face detector model: {model_name}"
        raise ValueError(msg)

    return detector_cls(
        model_path=model_path,
        score_threshold=score_threshold,
        nms_threshold=nms_threshold,
    )
