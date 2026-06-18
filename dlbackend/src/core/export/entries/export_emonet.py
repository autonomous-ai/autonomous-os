"""Export EmoNet (5 or 8 class) to ONNX."""

import argparse
import logging
from pathlib import Path

import torch
from typing_extensions import override

from core.export.components.emonet import EmoNet
from core.export.utils.constants import MODELS_DIR
from core.export.utils.evaluation import evaluate_image

logger: logging.Logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class EmoNetONNX(torch.nn.Module):
    def __init__(self, emonet: EmoNet):
        super().__init__()
        self.emonet: EmoNet = emonet

    @override
    def forward(self, x: torch.Tensor):
        out = self.emonet(x)
        return torch.softmax(out["expression"], dim=-1), out["valence"], out["arousal"]


def export(n_expression: int, output: str, opset: int = 11):
    state_dict_path = MODELS_DIR / "pretrained" / f"emonet_{n_expression}.pth"
    logger.info(f"Loading weights from {state_dict_path}")
    state_dict = torch.load(str(state_dict_path), map_location="cpu")
    state_dict = {k.replace("module.", ""): v for k, v in state_dict.items()}

    net = EmoNet(n_expression=n_expression)
    net.load_state_dict(state_dict, strict=False)
    net.eval()

    wrapper = EmoNetONNX(net)
    wrapper.eval()

    dummy = torch.rand(1, 3, 256, 256)

    logger.info(f"Exporting to {output}...")
    torch.onnx.export(
        wrapper,
        dummy,
        output,
        input_names=["images"],
        output_names=["probs", "valence", "arousal"],
        dynamic_axes={
            "images": {0: "batch_size"},
            "probs": {0: "batch_size"},
            "valence": {0: "batch_size"},
            "arousal": {0: "batch_size"},
        },
        opset_version=opset,
    )

    size_mb = Path(output).stat().st_size / 1024 / 1024
    logger.info(f"Exported to {output} ({size_mb:.1f} MB)")

    errors = evaluate_image(wrapper, Path(output), input_size=(256, 256))

    logger.info("Verification:")
    for i, e in enumerate(errors):
        logger.info(f"\tChannel {i}: mean_err = {e[0]:.6f} | max_err = {e[1]:.6f}")


def entry():
    logging.basicConfig(level=logging.DEBUG)

    parser = argparse.ArgumentParser(description="Export EmoNet to ONNX")
    parser.add_argument("--n-expression", type=int, default=5, choices=[5, 8])
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--opset", type=int, default=17)
    args = parser.parse_args()

    output = args.output or str(MODELS_DIR / "onnx" / f"emonet_{args.n_expression}.onnx")
    export(args.n_expression, output, args.opset)
