"""TCPFormer 3D pose lifter."""

from pathlib import Path

from core.enums.files import ModelEnum
from core.enums.pose import GraphEnum
from core.utils.files import get_default_cdn_url, get_default_model_path

from .base import PoseEstimator3DLifting


class TCPFormer3D(PoseEstimator3DLifting):
    """TCPFormer ONNX 3D pose lifter (H36M, 243 frames)."""

    GRAPH_TYPE: GraphEnum = GraphEnum.H36M

    DEFAULT_MODEL_PATH: Path | None = get_default_model_path(ModelEnum.TCPFORMER_H36M_243_ONNX)
    DEFAULT_REMOTE_URL: str | None = get_default_cdn_url(ModelEnum.TCPFORMER_H36M_243_ONNX)
    DEFAULT_N_FRAMES: int = 243
    DEFAULT_INPUT_SIZE: tuple[int, int] = (1920, 1080)
