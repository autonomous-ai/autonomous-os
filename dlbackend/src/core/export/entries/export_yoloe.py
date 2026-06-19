"""Export YOLO-E zero-shot object detector to ONNX.

The ONNX model includes the MobileCLIP text encoder so it takes raw
text token IDs as input, preserving zero-shot capability at runtime.

Inputs:
    images:    [batch, 3, H, W]           — preprocessed image
    class_tokens: [num_classes, context_len]  — tokenized class names

Outputs:
    boxes:   [batch, num_det, 4]  — center-based xywh, normalized [0,1]
    scores:  [batch, num_det]     — confidence scores
    labels:  [batch, num_det]     — class indices (-1 = padding)
"""

import argparse
import logging
from copy import deepcopy
from pathlib import Path
from typing import override

import torch
import torch.nn.functional as F
from ultralytics import YOLOE
from ultralytics.nn.text_model import build_text_model

from core.export.utils.constants import MODELS_DIR
from core.export.utils.evaluation import evaluate_image
from core.export.utils.nms import onnx_nms, xyxy_to_xywh_normalized

logger: logging.Logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class YOLOEONNX(torch.nn.Module):
    """Wraps YOLOEModel + MobileCLIP text encoder into a single traceable module."""

    def __init__(
        self, yoloe_model: torch.nn.Module, text_encoder: torch.nn.Module, nms: bool = True
    ):
        super().__init__()
        self.yoloe_model = yoloe_model
        self.text_encoder = text_encoder
        self.nms = nms

    @override
    def forward(self, images: torch.Tensor, class_tokens: torch.Tensor):
        txt_feats = self.text_encoder.encode_text(class_tokens)  # [K, 512]
        txt_feats = F.normalize(txt_feats, p=2, dim=-1)
        txt_feats = txt_feats.unsqueeze(0)  # [1, K, 512]

        raw = self.yoloe_model.predict(images, tpe=txt_feats)  # [B, 4+K, A]
        boxes_xywh = raw[:, :4, :].permute(0, 2, 1)  # [B, A, 4]
        scores = raw[:, 4:, :].permute(0, 2, 1)  # [B, A, K]

        # xywh → xyxy
        xy = boxes_xywh[..., :2]
        wh = boxes_xywh[..., 2:]
        boxes_xyxy = torch.cat([xy - wh / 2, xy + wh / 2], dim=-1)

        if self.nms:
            return onnx_nms(boxes_xyxy, scores, input_hw=(images.shape[2], images.shape[3]))

        xywh = xyxy_to_xywh_normalized(boxes_xyxy, input_hw=(images.shape[2], images.shape[3]))
        _, labels = scores.max(dim=-1)
        return xywh, scores, labels


def export(checkpoint: str, output: str, imgsz: int = 640, opset: int = 17, nms: bool = True):
    dest = Path(output).expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"Loading YOLO-E from {checkpoint}")
    yolo = YOLOE(checkpoint)

    # Extract detection model
    net = deepcopy(yolo.model).float()
    net.eval()
    net = net.fuse()
    for m in net.modules():
        if hasattr(m, "export"):
            m.export = True

    # Build text encoder
    text_model_name = getattr(net, "text_model", None) or "mobileclip:blt"
    logger.info(f"Building text encoder: {text_model_name}")
    text_model = build_text_model(text_model_name)

    wrapper = YOLOEONNX(net, text_model, nms=nms)
    wrapper.eval()

    # Dummy inputs: image + tokenized text
    dummy_images = torch.zeros(1, 3, imgsz, imgsz)
    dummy_class_tokens = text_model.tokenize(["person", "car", "dog"])

    if nms:
        output_names = ["boxes", "probs", "labels"]
        dynamic_axes = {
            "images": {0: "batch", 2: "height", 3: "width"},
            "class_tokens": {0: "num_classes"},
            "boxes": {0: "batch", 1: "num_det"},
            "probs": {0: "batch", 1: "num_det", 2: "num_classes"},
            "labels": {0: "batch", 1: "num_det"},
        }
    else:
        output_names = ["boxes", "probs", "labels"]
        dynamic_axes = {
            "images": {0: "batch", 2: "height", 3: "width"},
            "class_tokens": {0: "num_classes"},
            "boxes": {0: "batch"},
            "probs": {0: "batch", 2: "num_classes"},
        }

    logger.info(f"Exporting to {dest} (nms={nms})...")
    with torch.no_grad():
        torch.onnx.export(
            wrapper,
            (dummy_images, dummy_class_tokens),
            str(dest),
            input_names=["images", "class_tokens"],
            output_names=output_names,
            dynamic_axes=dynamic_axes,
            opset_version=opset,
            do_constant_folding=True,
        )

    size_mb = dest.stat().st_size / 1024 / 1024
    logger.info(f"Exported to {dest} ({size_mb:.1f} MB)")

    errors = evaluate_image(wrapper, dest, dummy_images.shape[-2:])

    logger.info("Verification:")
    for i, e in enumerate(errors):
        logger.info(f"\tChannel {i}: mean_err = {e[0]:.6f} | max_err = {e[1]:.6f}")


def entry():
    logging.basicConfig(level=logging.DEBUG)

    parser = argparse.ArgumentParser(description="Export YOLO-E to ONNX")
    parser.add_argument("--checkpoint", default="yoloe-26x-seg.pt")
    parser.add_argument("--output", default=None)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--opset", type=int, default=17)
    parser.add_argument("--nms", action="store_true", default=True)
    parser.add_argument("--no-nms", dest="nms", action="store_false")
    args = parser.parse_args()

    name = Path(args.checkpoint).stem
    suffix = "" if args.nms else "_raw"
    output = args.output or str(MODELS_DIR / "onnx" / f"{name}{suffix}.onnx")
    export(args.checkpoint, output, args.imgsz, args.opset, args.nms)
