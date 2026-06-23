"""EmoNet emotion predictor (5-class and 8-class variants).

Pure emotion classification from a face crop — no face detection.
Input: 256x256 RGB face crop, normalized to [0, 1].

Outputs expression logits + valence + arousal. Overrides
_postprocess_batch to extract the additional outputs.
"""

from pathlib import Path
from typing import cast

import numpy as np
import numpy.typing as npt
from typing_extensions import override

from core.enums.files import ModelEnum
from core.models.facial_emotion import RawEmotionDetection
from core.perception.facial_emotion.constants import RESOURCES_DIR
from core.perception.facial_emotion.predictors.base import EmotionRecognizer
from core.utils.files import get_default_cdn_url, get_default_model_path


class EmoNetRecognizer(EmotionRecognizer):
    """EmoNet ONNX emotion predictor. Supports 5-class and 8-class variants.

    Subclass only overrides class-level defaults and _postprocess_batch
    (EmoNet has 3 outputs: expression, valence, arousal).
    """

    DEFAULT_CLASSES_PATH_8: Path = RESOURCES_DIR / "emonet_8_classes.txt"
    DEFAULT_CLASSES_PATH_5: Path = RESOURCES_DIR / "emonet_5_classes.txt"

    DEFAULT_MODEL_PATH_8: Path = get_default_model_path(ModelEnum.EMONET_8_ONNX)
    DEFAULT_MODEL_PATH_5: Path = get_default_model_path(ModelEnum.EMONET_5_ONNX)

    DEFAULT_INPUT_SIZE: tuple[int, int] = (256, 256)

    # EmoNet normalizes to [0, 1] — mean=0, std=1 (div by 255 done in base preprocess)
    MEAN: npt.NDArray[np.float32] = np.array([0, 0, 0], dtype=np.float32)
    STD: npt.NDArray[np.float32] = np.array([1, 1, 1], dtype=np.float32)

    def __init__(
        self,
        n_expression: int = 8,
        model_path: Path | None = None,
        remote_url: str | None = None,
        classes_path: Path | None = None,
        batch_size: int | None = None,
    ) -> None:
        if n_expression not in (5, 8):
            msg: str = f"n_expression must be 5 or 8, got {n_expression}"
            raise ValueError(msg)

        if model_path is None:
            model_path = self.DEFAULT_MODEL_PATH_8 if n_expression == 8 else self.DEFAULT_MODEL_PATH_5
        if remote_url is None:
            model_enum = ModelEnum.EMONET_8_ONNX if n_expression == 8 else ModelEnum.EMONET_5_ONNX
            remote_url = get_default_cdn_url(model_enum)
        if classes_path is None:
            classes_path = self.DEFAULT_CLASSES_PATH_8 if n_expression == 8 else self.DEFAULT_CLASSES_PATH_5

        super().__init__(model_path=model_path, remote_url=remote_url, classes_path=classes_path, batch_size=batch_size)

    @override
    def _postprocess_batch(
        self, raw_outputs: list[npt.NDArray], N: int
    ) -> list[RawEmotionDetection]:
        """EmoNet has 3 outputs: probs (N, C), valence (N,), arousal (N,).

        Softmax is baked into the ONNX graph — probs are ready to use.
        """
        probs: npt.NDArray[np.float32] = cast(npt.NDArray[np.float32], raw_outputs[0])
        valence: npt.NDArray[np.float32] = cast(npt.NDArray[np.float32], raw_outputs[1])
        arousal: npt.NDArray[np.float32] = cast(npt.NDArray[np.float32], raw_outputs[2])

        return [
            RawEmotionDetection(
                expression_probs=probs[i],
                valence=float(valence[i]),
                arousal=float(arousal[i]),
            )
            for i in range(N)
        ]
