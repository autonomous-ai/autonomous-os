"""Emotion2Vec+ Large audio emotion recognizer."""

from pathlib import Path

from core.enums.files import ModelEnum
from core.perception.audio_emotion.constants import RESOURCES_DIR
from core.perception.audio_emotion.predictors.base import AudioEmotionRecognizer
from core.utils.files import get_default_cdn_url, get_default_model_path


class Emotion2VecPlusLargeRecognizer(AudioEmotionRecognizer):
    """Emotion2Vec+ Large — 9 emotion classes, 16kHz raw waveform input."""

    DEFAULT_MODEL_PATH: Path | None = get_default_model_path(ModelEnum.EMOTION2VEC_ONNX)
    DEFAULT_REMOTE_URL: str | None = get_default_cdn_url(ModelEnum.EMOTION2VEC_ONNX)
    DEFAULT_LABELS_PATH: Path | None = RESOURCES_DIR / "emotion2vec" / "labels.txt"
    DEFAULT_SAMPLE_RATE: int = 16000
    ONNX_INPUT_NAME: str = "audio"
    ONNX_OUTPUT_NAME: str = "probs"
