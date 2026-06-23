"""Export EmoNet (5 or 8 class) to ONNX."""

import argparse
import logging
from pathlib import Path

import torch
from typing_extensions import override

from core.enums.files import ModelEnum
from core.export.components.emonet import EmoNet
from core.export.utils.evaluation import evaluate_image
from core.export.utils.onnx import run_shape_inference
from core.utils.files import ensure_downloaded, get_default_cdn_url, get_default_model_path

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


def export(
    n_expression: int,
    checkpoint: str | None = None,
    output: str | None = None,
    opset: int = 11,
):
    model_enum_onnx = ModelEnum.EMONET_8_ONNX if n_expression == 8 else ModelEnum.EMONET_5_ONNX
    output = output or str(get_default_model_path(model_enum_onnx))
    dest = Path(output).expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    if checkpoint is None:
        model_enum = ModelEnum.EMONET_8_PTH if n_expression == 8 else ModelEnum.EMONET_5_PTH
        model_path = get_default_model_path(model_enum)
        remote_url = get_default_cdn_url(model_enum)
        state_dict_path = ensure_downloaded(model_path, remote=remote_url)
    else:
        state_dict_path = Path(checkpoint)

    logger.info(f"Loading weights from {state_dict_path}")
    state_dict = torch.load(str(state_dict_path), map_location="cpu", weights_only=True)
    state_dict = {k.replace("module.", ""): v for k, v in state_dict.items()}

    net = EmoNet(n_expression=n_expression)
    net.load_state_dict(state_dict, strict=False)
    net.eval()

    wrapper = EmoNetONNX(net)
    wrapper.eval()

    dummy = torch.rand(1, 3, 256, 256)

    logger.info(f"Exporting to {dest}...")
    torch.onnx.export(
        wrapper,
        dummy,
        str(dest),
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
    run_shape_inference(dest)

    size_mb = dest.stat().st_size / 1024 / 1024
    logger.info(f"Exported to {dest} ({size_mb:.1f} MB)")

    errors = evaluate_image(wrapper, dest, input_size=(256, 256))

    logger.info("Verification:")
    for i, e in enumerate(errors):
        logger.info(f"\tChannel {i}: mean_err = {e[0]:.6f} | max_err = {e[1]:.6f}")


def entry():
    logging.basicConfig(level=logging.DEBUG)

    parser = argparse.ArgumentParser(description="Export EmoNet to ONNX")
    parser.add_argument("--n-expression", type=int, default=5, choices=[5, 8])
    parser.add_argument("--checkpoint", type=str, default=None)
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--opset", type=int, default=17)
    args = parser.parse_args()

    export(args.n_expression, args.checkpoint, args.output, args.opset)
