"""Export Grounding DINO zero-shot object detector to ONNX.

The ONNX model includes the BERT text encoder so it takes raw text
token IDs as input, preserving zero-shot capability at runtime.

Inputs:
    images:       [batch, 3, H, W]       — preprocessed image
    class_tokens: [batch, seq_len]       — tokenized text prompt (e.g. "person . dog . cat .")

Outputs:
    boxes:   [batch, num_det, 4]           — center-based xywh, normalized [0,1]
    probs:   [batch, num_det, num_tokens]  — full score vector per detection
    labels:  [batch, num_det]              — argmax class indices (-1 = padding)
"""

import argparse
import logging
from pathlib import Path

import torch
from transformers import AutoProcessor, GroundingDinoForObjectDetection
from typing_extensions import override

from core.export.utils.constants import MODELS_DIR
from core.export.utils.evaluation import evaluate_image
from core.export.utils.nms import onnx_nms, xyxy_to_xywh_normalized

logger: logging.Logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class GroundingDINOONNX(torch.nn.Module):
    """Wraps Grounding DINO into the same interface as YOLO exports."""

    def __init__(self, model: GroundingDinoForObjectDetection, nms: bool = True):
        super().__init__()
        self.model = model
        self.nms = nms

    @override
    def forward(self, images: torch.Tensor, class_tokens: torch.Tensor):
        attention_mask = (class_tokens != 0).long()

        outputs = self.model(
            input_ids=class_tokens,
            attention_mask=attention_mask,
            pixel_values=images,
        )

        pred_boxes = outputs.pred_boxes  # [B, Q, 4] cxcywh
        probs = outputs.logits.sigmoid()  # [B, Q, K]

        # cxcywh → xyxy
        cx, cy, w, h = pred_boxes.unbind(-1)
        boxes_xyxy = torch.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], dim=-1)

        if self.nms:
            return onnx_nms(boxes_xyxy, probs, input_hw=(1, 1))

        xywh = xyxy_to_xywh_normalized(boxes_xyxy, input_hw=(1, 1))
        _, labels = probs.max(dim=-1)
        return xywh, probs, labels


def export(model_id: str, output: str | None = None, opset: int = 17, nms: bool = True):
    output = output or str(Path.cwd() / "grounding_dino.onnx")
    dest = Path(output).expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"Loading model from {model_id}...")
    model = GroundingDinoForObjectDetection.from_pretrained(model_id)
    model.eval()

    wrapper = GroundingDINOONNX(model, nms=nms)
    wrapper.eval()

    # Dummy inputs from processor
    processor = AutoProcessor.from_pretrained(model_id)
    dummy_text = "person . dog . cat ."
    dummy_inputs = processor(
        text=dummy_text,
        images=torch.zeros(3, 800, 800),
        return_tensors="pt",
    )

    dummy_images = dummy_inputs["pixel_values"]
    dummy_class_tokens = dummy_inputs["input_ids"]

    if nms:
        output_names = ["boxes", "probs", "labels"]
        dynamic_axes = {
            "images": {0: "batch", 2: "height", 3: "width"},
            "class_tokens": {0: "batch", 1: "seq_len"},
            "boxes": {0: "batch", 1: "num_det"},
            "probs": {0: "batch", 1: "num_det", 2: "num_tokens"},
            "labels": {0: "batch", 1: "num_det"},
        }
    else:
        output_names = ["boxes", "probs", "labels"]
        dynamic_axes = {
            "images": {0: "batch", 2: "height", 3: "width"},
            "class_tokens": {0: "batch", 1: "seq_len"},
            "boxes": {0: "batch"},
            "probs": {0: "batch", 2: "num_tokens"},
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

    parser = argparse.ArgumentParser(description="Export Grounding DINO to ONNX")
    parser.add_argument(
        "--model-id",
        default="IDEA-Research/grounding-dino-tiny",
        help="HuggingFace model identifier",
    )
    parser.add_argument("--output", default=None)
    parser.add_argument("--opset", type=int, default=17)
    parser.add_argument("--nms", action="store_true", default=True)
    parser.add_argument("--no-nms", dest="nms", action="store_false")
    args = parser.parse_args()

    suffix = "" if args.nms else "_raw"
    if args.output is None:
        name = args.model_id.split("/")[-1]
        args.output = str(MODELS_DIR / "onnx" / f"grounding_dino{suffix}.onnx")

    export(args.model_id, args.output, args.opset, args.nms)
