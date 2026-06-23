"""Export POSTER V2 (7-class RAF-DB) to ONNX."""

import argparse
import collections
import logging

# Inject unpickling stubs into __main__ so torch.load can resolve them
import sys as _sys
from pathlib import Path

import torch
from typing_extensions import override

from core.enums.files import ModelEnum
from core.export.components.posterv2 import Posterv2
from core.export.components.posterv2.utils import RecorderMeter, RecorderMeter1
from core.export.utils.evaluation import evaluate_image
from core.export.utils.onnx import run_shape_inference
from core.utils.files import ensure_downloaded, get_default_cdn_url, get_default_model_path

_main = _sys.modules["__main__"]
_main.RecorderMeter = RecorderMeter
_main.RecorderMeter1 = RecorderMeter1

logger: logging.Logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class Posterv2ONNX(torch.nn.Module):
    # ImageNet normalization (used by POSTER V2 training pipeline)
    MEAN: list[float] = [0.485, 0.456, 0.406]
    STD: list[float] = [0.229, 0.224, 0.225]

    def __init__(self, posterv2: Posterv2):
        super().__init__()
        self.posterv2: Posterv2 = posterv2
        self.register_buffer("mean", torch.tensor(self.MEAN).view(1, 3, 1, 1))
        self.register_buffer("std", torch.tensor(self.STD).view(1, 3, 1, 1))

    @override
    def forward(self, x: torch.Tensor):
        x = (x - self.mean) / self.std
        return torch.softmax(self.posterv2(x), dim=-1)


def export(checkpoint: str | None = None, output: str | None = None, num_classes: int = 7, opset: int = 17):
    output = output or str(get_default_model_path(ModelEnum.POSTERV2_ONNX))
    dest = Path(output).expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    if checkpoint is None:
        model_path = get_default_model_path(ModelEnum.POSTERV2_PTH)
        remote_url = get_default_cdn_url(ModelEnum.POSTERV2_PTH)
        checkpoint = str(ensure_downloaded(model_path, remote=remote_url))

    ckpt = torch.load(checkpoint, map_location="cpu", weights_only=False)
    state_dict = ckpt.get("state_dict", ckpt)
    clean = collections.OrderedDict()
    for k, v in state_dict.items():
        clean[k.removeprefix("module.")] = v

    net = Posterv2(img_size=224, num_classes=num_classes)
    missing, unexpected = net.load_state_dict(clean, strict=False)
    if missing:
        logger.warning(f"missing keys ({len(missing)}): {missing[:5]}...")
    if unexpected:
        logger.warning(f"unexpected keys ({len(unexpected)}): {unexpected[:5]}...")
    net.eval()

    wrapper = Posterv2ONNX(net)
    wrapper.eval()

    dummy = torch.randn(1, 3, 224, 224)

    logger.info(f"Exporting to {dest}...")
    torch.onnx.export(
        wrapper,
        dummy,
        str(dest),
        opset_version=opset,
        input_names=["images"],
        output_names=["probs"],
        dynamic_axes={"images": {0: "batch"}, "probs": {0: "batch"}},
    )
    run_shape_inference(dest)

    size_mb = dest.stat().st_size / 1024 / 1024
    logger.info(f"Exported to {dest} ({size_mb:.1f} MB)")

    errors = evaluate_image(wrapper, dest, input_size=(224, 224))

    logger.info("Verification:")
    for i, e in enumerate(errors):
        logger.info(f"\tChannel {i}: mean_err = {e[0]:.6f} | max_err = {e[1]:.6f}")


def entry():
    logging.basicConfig(level=logging.DEBUG)

    parser = argparse.ArgumentParser(description="Export POSTER V2 to ONNX")
    parser.add_argument("--checkpoint", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--num-classes", type=int, default=7)
    parser.add_argument("--opset", type=int, default=17)
    args = parser.parse_args()

    export(args.checkpoint, args.output, args.num_classes, args.opset)
