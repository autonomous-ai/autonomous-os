"""Run all ONNX exports."""

import logging
from pathlib import Path

from core.export.components.uniformerv2.model import CHECKPOINT_MAP
from core.export.utils.constants import MODELS_DIR

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
    export_yoloe,
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


def export_all():
    results: dict[str, bool] = {}

    # EmoNet
    for n in (5, 8):
        ckpt = MODELS_DIR / "pretrained" / f"emonet_{n}.pth"
        if ckpt.exists():
            output = str(MODELS_DIR / "onnx" / f"emonet_{n}.onnx")
            results[f"emonet_{n}"] = _run(
                f"emonet_{n}",
                lambda n=n, o=output: export_emonet.export(n, o),
            )
        else:
            logger.info("[skip] EmoNet %d: %s not found", n, ckpt)

    # POSTER V2
    ckpt = MODELS_DIR / "pretrained" / "posterv2_7cls.pth"
    if ckpt.exists():
        output = str(MODELS_DIR / "onnx" / "posterv2_7cls.onnx")
        results["posterv2"] = _run(
            "posterv2",
            lambda: export_posterv2.export(str(ckpt), output),
        )
    else:
        logger.info("[skip] POSTER V2: %s not found", ckpt)

    # TCPFormer
    ckpt = MODELS_DIR / "pretrained" / "TCPFormer_h36m_243_379.pth.tr"
    if ckpt.exists():
        output = str(MODELS_DIR / "onnx" / "tcpformer_h36m_243.onnx")
        results["tcpformer"] = _run(
            "tcpformer",
            lambda: export_tcpformer.export(str(ckpt), output),
        )
    else:
        logger.info("[skip] TCPFormer: %s not found", ckpt)

    # Emotion2Vec
    results["emotion2vec"] = _run(
        "emotion2vec",
        lambda: export_emotion2vec.export("iic/emotion2vec_plus_large"),
    )

    # UniformerV2
    for config_name, ckpt_name in CHECKPOINT_MAP.items():
        ckpt = MODELS_DIR / "pretrained" / ckpt_name
        if ckpt.exists():
            output = str(MODELS_DIR / "onnx" / f"{Path(ckpt_name).stem}_fp32.onnx")
            results[f"uniformerv2-{config_name}"] = _run(
                f"uniformerv2-{config_name}",
                lambda c=config_name, k=str(ckpt), o=output: export_uniformerv2.export(c, k, o),
            )
        else:
            logger.info("[skip] UniformerV2 %s: %s not found", config_name, ckpt)

    # OWLv2
    results["owlv2"] = _run(
        "owlv2",
        lambda: export_owlv2.export("google/owlv2-large-patch14-ensemble"),
        required=False,
    )

    # Grounding DINO
    results["grounding_dino"] = _run(
        "grounding_dino",
        lambda: export_grounding_dino.export("IDEA-Research/grounding-dino-tiny"),
        required=False,
    )

    # YOLO (person detection)
    ckpt = MODELS_DIR / "pretrained" / "yolo12x.pt"
    if ckpt.exists():
        output = str(MODELS_DIR / "onnx" / "yolo12x.onnx")
        results["yolo"] = _run(
            "yolo",
            lambda: export_yolo.export(str(ckpt), output),
            required=False,
        )
    else:
        logger.info("[skip] YOLO: %s not found", ckpt)

    # YOLO-World
    ckpt = MODELS_DIR / "pretrained" / "yolov8x-worldv2.pt"
    if ckpt.exists():
        output = str(MODELS_DIR / "onnx" / "yolov8x-worldv2.onnx")
        results["yolo_world"] = _run(
            "yolo_world",
            lambda: export_yolo_world.export(str(ckpt), output),
            required=False,
        )
    else:
        logger.info("[skip] YOLO-World: %s not found", ckpt)

    # YOLO-E
    ckpt = MODELS_DIR / "pretrained" / "yoloe-26x-seg.pt"
    if ckpt.exists():
        output = str(MODELS_DIR / "onnx" / "yoloe-26x-seg.onnx")
        results["yoloe"] = _run(
            "yoloe",
            lambda: export_yoloe.export(str(ckpt), output),
            required=False,
        )
    else:
        logger.info("[skip] YOLO-E: %s not found", ckpt)

    # Summary
    logger.info("=" * 60)
    logger.info("Summary:")
    logger.info("=" * 60)
    for name, ok in results.items():
        status = "PASSED" if ok else "FAILED"
        logger.info("  %-20s %s", name, status)


def entry():
    logging.basicConfig(level=logging.INFO)
    export_all()
