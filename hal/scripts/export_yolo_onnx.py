#!/usr/bin/env python3
"""Export the checked-in yolov8n.pt to ONNX for the tracking detector.

The .pt runs through PyTorch, which is the slowest way to execute this graph on
the device CPU. onnxruntime is already a HAL dependency, so the same weights in
ONNX cost nothing extra and leave room in the latency budget for a larger
inference size — which is what small desk objects (cup, book, phone) need.

The input size is baked into the export, so this reads it from the detector
rather than taking a flag: the two cannot drift apart.

    uv run python hal/scripts/export_yolo_onnx.py

Writes hal/drivers/tracking/models/yolov8n.onnx. The detector prefers that file
and falls back to the .pt when it is absent, so exporting is safe to redo and
safe to skip.
"""

import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from hal.drivers.tracking.detection import (  # noqa: E402
    _LOCAL_IMGSZ as IMGSZ,
    _LOCAL_MODEL_ONNX as OUT,
    _LOCAL_MODEL_PT as SRC,
)


def main() -> int:
    if not os.path.exists(SRC):
        print(f"missing source weights: {SRC}", file=sys.stderr)
        return 1

    from ultralytics import YOLO

    print(f"exporting {SRC} -> ONNX at imgsz={IMGSZ}")
    # simplify=True folds the graph so onnxruntime does not redo it per session.
    # It needs onnxslim, which is not a HAL dependency; ultralytics warns and
    # exports unsimplified when it is missing. That is a small speed loss, not
    # a broken model, so this deliberately does not require the extra package.
    produced = YOLO(SRC).export(format="onnx", imgsz=IMGSZ, simplify=True)

    # ultralytics writes next to the source and names the file itself; move it
    # to the exact path the detector looks for.
    if os.path.abspath(produced) != os.path.abspath(OUT):
        shutil.move(produced, OUT)
    print(f"wrote {OUT} ({os.path.getsize(OUT) / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
