"""Factory functions and factory classes for action recognizer models."""

from pathlib import Path

from core.enums import HumanActionRecognizerEnum
from core.perception.action.predictors.base import HumanActionRecognizer
from core.perception.base import PredictorFactory


class ActionRecognizerFactory(PredictorFactory[HumanActionRecognizer]):
    """Factory that creates HumanActionRecognizer instances from config."""

    def __init__(
        self,
        model_name: HumanActionRecognizerEnum,
        model_path: Path | None = None,
        max_frames: int | None = None,
        frame_size: tuple[int, int] | None = None,
    ) -> None:
        self._model_name = model_name
        self._model_path = model_path
        self._max_frames = max_frames
        self._frame_size = frame_size

    def create(self) -> HumanActionRecognizer:
        return create_recognizer(
            self._model_name, self._model_path,
            max_frames=self._max_frames, frame_size=self._frame_size,
        )


def create_recognizer(
    model_name: HumanActionRecognizerEnum,
    model_path: Path | None,
    max_frames: int | None = None,
    frame_size: tuple[int, int] | None = None,
) -> HumanActionRecognizer:
    """Instantiate the correct recognizer model."""
    if model_name == HumanActionRecognizerEnum.VIDEOMAE:
        from core.perception.action.predictors.videomae import VideoMAEModel as recognizer_cls
    elif model_name == HumanActionRecognizerEnum.UNIFORMERV2:
        from core.perception.action.predictors.uniformerv2 import UniformerV2Model as recognizer_cls
    elif model_name == HumanActionRecognizerEnum.X3D:
        from core.perception.action.predictors.x3d import X3DModel as recognizer_cls
    else:
        msg = f"Unknown action recognition model: {model_name}"
        raise ValueError(msg)

    return recognizer_cls(model_path, max_frames=max_frames, frame_size=frame_size)
