"""Export TCPFormer (H36M 243-frame) to ONNX."""

import argparse
import logging
from pathlib import Path

import torch
from typing_extensions import override

from core.export.components.tcpformer import MemoryInducedTransformer, build_model
from core.export.utils.constants import MODELS_DIR
from core.export.utils.evaluation import evaluate_skeleton

logger: logging.Logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class TCPFormerONNX(torch.nn.Module):
    def __init__(self, tcpformer: MemoryInducedTransformer):
        super().__init__()
        self.tcpformer: MemoryInducedTransformer = tcpformer

    @override
    def forward(self, x: torch.Tensor):
        return self.tcpformer(x)


def export(checkpoint: str, output: str, opset: int = 17):
    dest = Path(output).expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

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

    size_mb = dest.stat().st_size / 1024 / 1024
    logger.info(f"Exported to {dest} ({size_mb:.1f} MB)")

    errors = evaluate_skeleton(wrapper, dest, input_shape=(243, 17, 3))

    logger.info("Verification:")
    for i, e in enumerate(errors):
        logger.info(f"\tChannel {i}: mean_err = {e[0]:.6f} | max_err = {e[1]:.6f}")


def entry():
    logging.basicConfig(level=logging.DEBUG)

    parser = argparse.ArgumentParser(description="Export TCPFormer to ONNX")
    parser.add_argument(
        "--checkpoint", default=str(MODELS_DIR / "pretrained" / "TCPFormer_h36m_243_379.pth.tr")
    )
    parser.add_argument("--output", default=None)
    parser.add_argument("--opset", type=int, default=17)
    args = parser.parse_args()

    output = args.output or str(MODELS_DIR / "onnx" / "tcpformer_h36m_243.onnx")
    export(args.checkpoint, output, args.opset)
