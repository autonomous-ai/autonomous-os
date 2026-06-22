"""Export Emotion2Vec SER model to ONNX."""

import argparse
import logging
import os
import types
from pathlib import Path

import torch
from typing_extensions import override

from core.enums.files import ModelEnum
from core.export.components.emotion2vec import Emotion2vec
from core.export.utils.evaluation import evaluate_audio
from core.utils.files import get_default_model_path

logger: logging.Logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _normalize_label(raw):
    line = raw.strip()
    if not line:
        return None
    if "/" in line:
        parts = [p.strip() for p in line.split("/") if p.strip()]
        if parts:
            return parts[-1]
    return line


def _materialize_labels(token_list: list[str], output_dir: Path):
    """Write labels.txt next to the ONNX file."""
    labels = [lbl for lbl in (_normalize_label(tok) for tok in token_list) if lbl]
    if not labels:
        logger.warning("No labels materialized. Provide labels.txt manually.")
        return None

    labels_dest = output_dir / "labels.txt"
    labels_dest.write_text("\n".join(labels) + "\n", encoding="utf-8")
    logger.info(f"Wrote {len(labels)} labels -> {labels_dest}")
    return labels_dest


def _export_forward(self, x):
    with torch.no_grad():
        if self.cfg.normalize:
            mean = torch.mean(x, dim=1, keepdim=True)
            var = torch.var(x, dim=1, keepdim=True, unbiased=False)
            x = (x - mean) / torch.sqrt(var + 1e-5)
            x = x.view(x.shape[0], -1)

        res = self._original_forward(
            source=x,
            padding_mask=None,
            mask=False,
            features_only=True,
            remove_extra_tokens=True,
        )
        h = res["x"]
        if self.proj is None:
            return h

        z = h.mean(dim=1)
        logits = self.proj(z)
        if getattr(self, "_export_logit_bias", None) is not None:
            logits = logits + self._export_logit_bias.to(device=logits.device, dtype=logits.dtype)
        return logits


def _rebuild_for_export(model, token_list: list[str] | None = None):
    model._original_forward = model.forward

    if getattr(model, "proj", None) is not None:
        n = model.proj.out_features
        bias = torch.zeros(n, dtype=torch.float32)
        if token_list is not None and len(token_list) == n:
            for i, lab in enumerate(token_list):
                if str(lab).startswith("unuse"):
                    bias[i] = -1e4
        model.register_buffer("_export_logit_bias", bias)

    model.forward = types.MethodType(_export_forward, model)
    return model


class Emotion2VecONNX(torch.nn.Module):
    def __init__(self, model: torch.nn.Module):
        super().__init__()
        self.model = model

    @override
    def forward(self, x: torch.Tensor):
        return torch.softmax(self.model(x), dim=-1)


def export(model_id: str, output: str | None = None, opset: int = 17):
    output = output or str(get_default_model_path(ModelEnum.EMOTION2VEC_ONNX))
    dest = Path(output).expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"Loading weights from HuggingFace (model_id={model_id})...")
    net, token_list = Emotion2vec.load_from_hf(model_id)

    if getattr(net, "proj", None) is None:
        raise RuntimeError(
            f"Model '{model_id}' has no classifier head (proj layer). "
            "Use a variant with a classifier (e.g. emotion2vec/emotion2vec_plus_large)."
        )

    with torch.no_grad():
        rebuilt = _rebuild_for_export(net, token_list=token_list)
        rebuilt.eval()

        wrapper = Emotion2VecONNX(rebuilt)
        wrapper.eval()

        # Use a long dummy input (30 seconds) so the ALiBi attention bias
        # is traced at a size large enough to handle real audio.  Shorter
        # inputs at runtime are handled by the [:T, :T] slicing inside the
        # attention module.
        dummy = torch.randn(1, 16000 * 30)
        intermediate = dest.parent / "emotion2vec.onnx"

        logger.info(f"Exporting to {dest}...")
        torch.onnx.export(
            wrapper,
            (dummy,),
            str(intermediate),
            do_constant_folding=True,
            input_names=["audio"],
            output_names=["probs"],
            dynamic_axes={
                "audio": {0: "batch_size", 1: "sequence_length"},
                "probs": {0: "batch_size"},
            },
            opset_version=opset,
        )

    if intermediate != dest:
        if dest.exists():
            dest.unlink()
        os.replace(intermediate, dest)

    _materialize_labels(token_list, dest.parent)

    size_mb = dest.stat().st_size / 1024 / 1024
    logger.info(f"Exported to {dest} ({size_mb:.1f} MB)")

    errors = evaluate_audio(wrapper, dest)

    logger.info("Verification:")
    for i, e in enumerate(errors):
        logger.info(f"\tChannel {i}: mean_err = {e[0]:.6f} | max_err = {e[1]:.6f}")


def entry():
    logging.basicConfig(level=logging.DEBUG)

    parser = argparse.ArgumentParser(description="Export Emotion2Vec SER to ONNX")
    parser.add_argument(
        "--model-id",
        default="emotion2vec/emotion2vec_plus_large",
        help="FunASR / HuggingFace model identifier",
    )
    parser.add_argument("--output", default=None)
    parser.add_argument("--opset", type=int, default=17)
    args = parser.parse_args()

    export(args.model_id, args.output, args.opset)
