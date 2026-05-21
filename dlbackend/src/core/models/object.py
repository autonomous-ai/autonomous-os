"""Internal object detection models — dataclasses for core logic, not HTTP."""

from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt


@dataclass
class RawObjectDetection:
    """Raw object detector output — batched numpy arrays."""

    bbox_xywh: npt.NDArray[np.float32]
    """Shape: (N, 4) — center_x, center_y, width, height per detection."""

    class_names: list[str]
    """Length N — class name per detection."""

    confidence: npt.NDArray[np.float32]
    """Shape: (N,) — confidence per detection."""


@dataclass
class ObjectDetectionItem:
    """Single detected object."""

    class_name: str
    xywh: list[float]
    confidence: float


@dataclass
class ObjectDetection:
    """Session output: object detection result for a single frame."""

    detections: list[ObjectDetectionItem] = field(default_factory=list)


@dataclass
class ObjectPerceptionSessionConfig:
    frame_interval: float = 0.0
    classes: list[str] | None = None
    threshold: float = 0.25
