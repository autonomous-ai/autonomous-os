"""UniformerV2 action recognizer model."""

from pathlib import Path

import numpy as np
import numpy.typing as npt

from core.enums.files import ModelEnum
from core.perception.action.constants import RESOURCES_DIR
from core.perception.action.predictors.base import HumanActionRecognizer
from core.utils.files import get_default_cdn_url, get_default_model_path


class UniformerV2Model(HumanActionRecognizer):
    """UniformerV2 ONNX model for action recognition."""

    DEFAULT_MODEL_PATH: Path | None = get_default_model_path(ModelEnum.UNIFORMERV2)
    DEFAULT_REMOTE_URL: str | None = get_default_cdn_url(ModelEnum.UNIFORMERV2)
    DEFAULT_CLASSES_PATH: Path = RESOURCES_DIR / "kinect_classes.txt"
    DEFAULT_WHITELIST_PATH: Path | None = RESOURCES_DIR / "white_list.txt"

    DEFAULT_MAX_FRAMES: int = 8
    DEFAULT_FRAME_SIZE: tuple[int, int] = (224, 224)
    ONNX_INPUT_NAME: str = "videos"
    ONNX_OUTPUT_NAME: str = "probs"

    # Identity — normalization and softmax are baked into the ONNX export wrapper.
    MEAN: npt.NDArray[np.float32] = np.array([0, 0, 0], dtype=np.float32)
    STD: npt.NDArray[np.float32] = np.array([1, 1, 1], dtype=np.float32)
