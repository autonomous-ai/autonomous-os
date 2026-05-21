from pathlib import Path

from core.enums import EmotionRecognizerEnum
from core.perception.base import PredictorFactory
from core.perception.emotion.predictors.base import EmotionRecognizer


class EmotionRecognizerFactory(PredictorFactory[EmotionRecognizer]):
    """Factory that creates EmotionRecognizer instances from config."""

    def __init__(
        self,
        model_name: EmotionRecognizerEnum,
        model_path: Path | None = None,
    ) -> None:
        self._model_name = model_name
        self._model_path = model_path

    def create(self) -> EmotionRecognizer:
        return create_emotion_recognizer(self._model_name, self._model_path)


def create_emotion_recognizer(
    model_name: EmotionRecognizerEnum,
    model_path: Path | None = None,
) -> EmotionRecognizer:
    """Create the emotion recognizer for the given model type."""
    if model_name == EmotionRecognizerEnum.POSTERV2:
        from core.perception.emotion.predictors.posterv2 import PosterV2Recognizer

        return PosterV2Recognizer(model_path=model_path)
    elif model_name == EmotionRecognizerEnum.EMONET_8:
        from core.perception.emotion.predictors.emonet import EmoNetRecognizer

        return EmoNetRecognizer(n_expression=8, model_path=model_path)
    elif model_name == EmotionRecognizerEnum.EMONET_5:
        from core.perception.emotion.predictors.emonet import EmoNetRecognizer

        return EmoNetRecognizer(n_expression=5, model_path=model_path)
    else:
        msg = f"Unknown emotion recognition model: {model_name}"
        raise ValueError(msg)
