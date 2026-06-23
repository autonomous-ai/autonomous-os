"""Export UniformerV2 (video action recognition) to ONNX."""

import argparse
import logging
from pathlib import Path

import torch
from typing_extensions import override

from core.enums.files import ModelEnum
from core.export.components.uniformerv2 import UniformerV2
from core.export.components.uniformerv2.model import CONFIGS
from core.export.utils.evaluation import evaluate_video
from core.export.utils.onnx import run_shape_inference
from core.utils.files import ensure_downloaded, get_default_cdn_url, get_default_model_path

logger: logging.Logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class UniformerV2ONNX(torch.nn.Module):
    # Original expects [0,255]: (x - 114.75) / 57.375
    # For [0,1] input: (x - 114.75/255) / (57.375/255)
    MEAN: list[float] = [114.75 / 255, 114.75 / 255, 114.75 / 255]
    STD: list[float] = [57.375 / 255, 57.375 / 255, 57.375 / 255]

    def __init__(self, model: UniformerV2):
        super().__init__()
        self.model = model
        self.register_buffer("mean", torch.tensor(self.MEAN).view(1, 1, 3, 1, 1, 1))
        self.register_buffer("std", torch.tensor(self.STD).view(1, 1, 3, 1, 1, 1))

    @override
    def forward(self, x: torch.Tensor):
        x = (x - self.mean) / self.std
        return torch.softmax(self.model(x), dim=-1)


def export(config_name: str, checkpoint: str | None = None, output: str | None = None, opset: int = 17):
    output = output or str(get_default_model_path(ModelEnum.UNIFORMERV2_ONNX))
    dest = Path(output).expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    if checkpoint is None:
        model_path = get_default_model_path(ModelEnum.UNIFORMERV2_PTH)
        remote_url = get_default_cdn_url(ModelEnum.UNIFORMERV2_PTH)
        checkpoint = str(ensure_downloaded(model_path, remote=remote_url))

    logger.info(f"Loading weights from {checkpoint}")
    net = UniformerV2.load_from_checkpoint(config_name, Path(checkpoint))
    net.eval()

    wrapper = UniformerV2ONNX(net)
    wrapper.eval()

    # (batch, clips, C, T, H, W)
    cfg = CONFIGS[config_name]
    res = cfg["input_resolution"]
    dummy = torch.randn(1, 1, 3, cfg["t_size"], res, res)

    logger.info(f"Exporting to {dest}...")
    torch.onnx.export(
        wrapper,
        dummy,
        str(dest),
        input_names=["videos"],
        output_names=["probs"],
        dynamic_axes={
            "videos": {0: "batch", 1: "clip"},
            "probs": {0: "batch"},
        },
        opset_version=opset,
        do_constant_folding=True,
    )
    run_shape_inference(dest)

    size_mb = dest.stat().st_size / 1024 / 1024
    logger.info(f"Exported to {dest} ({size_mb:.1f} MB)")

    # input_shape excludes batch dim: (clips, C, T, H, W)
    errors = evaluate_video(wrapper, dest, input_shape=(1, 3, cfg["t_size"], res, res))

    logger.info("Verification:")
    for i, e in enumerate(errors):
        logger.info(f"\tChannel {i}: mean_err = {e[0]:.6f} | max_err = {e[1]:.6f}")


def entry():
    logging.basicConfig(level=logging.DEBUG)

    parser = argparse.ArgumentParser(description="Export UniformerV2 to ONNX")
    parser.add_argument(
        "--config",
        default="large-k400",
        choices=list(CONFIGS.keys()),
        help="Model config preset",
    )
    parser.add_argument("--checkpoint", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--opset", type=int, default=17)
    args = parser.parse_args()

    export(args.config, args.checkpoint, args.output, args.opset)
