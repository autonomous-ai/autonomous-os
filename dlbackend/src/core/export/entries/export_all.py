"""Run all ONNX exports.

Each individual export function handles checkpoint resolution internally
via ensure_downloaded — no need to check for local pretrained files here.
"""

import logging
from pathlib import Path

from . import (
    export_emonet,
    export_emotion2vec,
    export_grounding_dino,
    export_owlv2,
    export_posterv2,
    export_tcpformer,
    export_uniformerv2,
    export_yolo,
    export_yolo_world,
)

logger: logging.Logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _run(name: str, fn, required: bool = True) -> bool:
    logger.info("=" * 60)
    logger.info("Running: %s", name)
    logger.info("=" * 60)
    try:
        fn()
        return True
    except Exception:
        if required:
            logger.exception("FAILED: %s", name)
            return False
        logger.warning("SKIPPED (optional): %s", name)
        return True


def _output(output_dir: Path | None, filename: str) -> str | None:
    """Build output path if output_dir is set, otherwise None (use default)."""
    if output_dir is None:
        return None
    return str(output_dir / filename)


def export_all(output_dir: Path | None = None):
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, bool] = {}

    # EmoNet
    for n in (5, 8):
        results[f"emonet_{n}"] = _run(
            f"emonet_{n}",
            lambda n=n: export_emonet.export(n, output=_output(output_dir, f"emonet_{n}.onnx")),
        )

    # POSTER V2
    results["posterv2"] = _run(
        "posterv2",
        lambda: export_posterv2.export(output=_output(output_dir, "posterv2_7cls.onnx")),
    )

    # TCPFormer
    results["tcpformer"] = _run(
        "tcpformer",
        lambda: export_tcpformer.export(output=_output(output_dir, "tcpformer_h36m_243.onnx")),
    )

    # Emotion2Vec
    results["emotion2vec"] = _run(
        "emotion2vec",
        lambda: export_emotion2vec.export(
            "emotion2vec/emotion2vec_plus_large",
            output=_output(output_dir, "emotion2vec.onnx"),
        ),
    )

    # UniformerV2
    results["uniformerv2"] = _run(
        "uniformerv2",
        lambda: export_uniformerv2.export(
            "large-k400", output=_output(output_dir, "uniformerv2-l-224-k400_fp32.onnx"),
        ),
    )

    # OWLv2
    results["owlv2"] = _run(
        "owlv2",
        lambda: export_owlv2.export(
            "google/owlv2-large-patch14-ensemble",
            output=_output(output_dir, "owlv2_raw.onnx"),
            nms=False,
        ),
        required=False,
    )

    # Grounding DINO
    results["grounding_dino"] = _run(
        "grounding_dino",
        lambda: export_grounding_dino.export(
            "IDEA-Research/grounding-dino-tiny",
            output=_output(output_dir, "grounding_dino_raw.onnx"),
            nms=False,
        ),
        required=False,
    )

    # YOLO (person detection)
    results["yolo"] = _run(
        "yolo",
        lambda: export_yolo.export(
            output=_output(output_dir, "yolo12x_raw.onnx"), nms=False,
        ),
        required=False,
    )

    # YOLO-World
    results["yolo_world"] = _run(
        "yolo_world",
        lambda: export_yolo_world.export(
            output=_output(output_dir, "yolov8x-worldv2_raw.onnx"), nms=False,
        ),
        required=False,
    )

    # Summary
    logger.info("=" * 60)
    logger.info("Summary:")
    logger.info("=" * 60)
    for name, ok in results.items():
        status = "PASSED" if ok else "FAILED"
        logger.info("  %-20s %s", name, status)


def entry():
    import argparse

    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Export all models to ONNX")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Directory for ONNX outputs. If omitted, uses default model cache path.")
    args = parser.parse_args()

    export_all(output_dir=args.output_dir)
