#!/usr/bin/env python3
"""Re-export the tracking detector's YOLO model to ONNX.

The repo ships the model as ONNX only: it runs through onnxruntime, already a
HAL dependency, rather than PyTorch, which is the slowest way to execute this
graph on the device CPU. That speed is what pays for the inference size small
desk objects need.

You only need this when changing `_LOCAL_IMGSZ`, since the export bakes the
input size in. The source .pt is not kept in the repo — fetch it first:

    curl -LO https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.pt
    uv run python hal/scripts/export_yolo_onnx.py yolov8n.pt

The size is read from the detector rather than taken as a flag, so the two
cannot drift apart. Overwrites the checked-in model in place; commit the result.
"""

import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from hal.drivers.tracking.detection import (  # noqa: E402
    _LOCAL_IMGSZ as IMGSZ,
    _LOCAL_MODEL_PATH as OUT,
)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    src = argv[0]
    if not os.path.exists(src):
        print(f"missing source weights: {src}", file=sys.stderr)
        return 1

    from ultralytics import YOLO

    print(f"exporting {src} -> ONNX at imgsz={IMGSZ}")
    # simplify=True folds the graph so onnxruntime does not redo it per session.
    # It needs onnxslim, which is not a HAL dependency; ultralytics warns and
    # exports unsimplified when it is missing. That is a small speed loss, not
    # a broken model, so this deliberately does not require the extra package.
    produced = YOLO(src).export(format="onnx", imgsz=IMGSZ, simplify=True)

    # ultralytics writes next to the source and names the file itself; move it
    # to the exact path the detector loads.
    if os.path.abspath(produced) != os.path.abspath(OUT):
        shutil.move(produced, OUT)
    print(f"wrote {OUT} ({os.path.getsize(OUT) / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
