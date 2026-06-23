"""Export TCPFormer (H36M 243-frame) to ONNX."""

import argparse
import logging
from pathlib import Path

import torch
from typing_extensions import override

from core.enums.files import ModelEnum
from core.export.components.tcpformer import MemoryInducedTransformer, build_model
from core.export.utils.evaluation import evaluate_skeleton
from core.export.utils.onnx import run_shape_inference
from core.utils.files import ensure_downloaded, get_default_cdn_url, get_default_model_path

logger: logging.Logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class TCPFormerONNX(torch.nn.Module):
    def __init__(self, tcpformer: MemoryInducedTransformer):
        super().__init__()
        self.tcpformer: MemoryInducedTransformer = tcpformer

    @override
    def forward(self, x: torch.Tensor):
        return self.tcpformer(x)


def export(checkpoint: str | None = None, output: str | None = None, opset: int = 17):
    output = output or str(get_default_model_path(ModelEnum.TCPFORMER_H36M_243_ONNX))
    dest = Path(output).expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    if checkpoint is None:
        model_path = get_default_model_path(ModelEnum.TCPFORMER_H36M_243_PTH)
        remote_url = get_default_cdn_url(ModelEnum.TCPFORMER_H36M_243_PTH)
        checkpoint = str(ensure_downloaded(model_path, remote=remote_url))

    state_dict_path = Path(checkpoint)
    logger.info(f"Loading weights from {state_dict_path}")
    ckpt = torch.load(str(state_dict_path), map_location="cpu", weights_only=False)
    state_dict = ckpt.get("model", ckpt)
    state_dict = {k.removeprefix("module."): v for k, v in state_dict.items()}

    net = build_model()
    net.load_state_dict(state_dict, strict=False)
    net.eval()

    wrapper = TCPFormerONNX(net)
    wrapper.eval()

    dummy = torch.randn(1, 243, 17, 3)

    logger.info(f"Exporting to {dest}...")
    torch.onnx.export(
        wrapper,
        dummy,
        str(dest),
        input_names=["keypoints"],
        output_names=["poses"],
        dynamic_axes={
            "keypoints": {0: "batch_size"},
            "poses": {0: "batch_size"},
        },
        opset_version=opset,
    )
    run_shape_inference(dest)

    size_mb = dest.stat().st_size / 1024 / 1024
    logger.info(f"Exported to {dest} ({size_mb:.1f} MB)")

    errors = evaluate_skeleton(wrapper, dest, input_shape=(243, 17, 3))

    logger.info("Verification:")
    for i, e in enumerate(errors):
        logger.info(f"\tChannel {i}: mean_err = {e[0]:.6f} | max_err = {e[1]:.6f}")


def entry():
    logging.basicConfig(level=logging.DEBUG)

    parser = argparse.ArgumentParser(description="Export TCPFormer to ONNX")
    parser.add_argument("--checkpoint", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--opset", type=int, default=17)
    args = parser.parse_args()

    export(args.checkpoint, args.output, args.opset)
