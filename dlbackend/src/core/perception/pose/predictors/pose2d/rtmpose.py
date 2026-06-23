"""RTMPose 2D pose estimator."""

from pathlib import Path

import numpy as np
import numpy.typing as npt

from core.enums.files import ModelEnum
from core.enums.pose import GraphEnum
from core.utils.files import get_default_cdn_url, get_default_model_path

from .base import PoseEstimator2D


class RTMPose2D(PoseEstimator2D):
    """RTMPose ONNX 2D pose estimator using SimCC coordinate decoding."""

    GRAPH_TYPE: GraphEnum = GraphEnum.COCO

    DEFAULT_MODEL_PATH: Path | None = get_default_model_path(ModelEnum.RTMPOSE_M_ONNX)
    DEFAULT_REMOTE_URL: str | None = get_default_cdn_url(ModelEnum.RTMPOSE_M_ONNX)
    DEFAULT_INPUT_SIZE: tuple[int, int] = (192, 256)

    INPUT_MEAN: npt.NDArray[np.float32] = np.array([123.675, 116.28, 103.53], dtype=np.float32)
    INPUT_STD: npt.NDArray[np.float32] = np.array([58.395, 57.12, 57.375], dtype=np.float32)
