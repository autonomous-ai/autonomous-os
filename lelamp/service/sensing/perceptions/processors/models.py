"""Data models for perception processors."""

from dataclasses import dataclass
from enum import StrEnum

import numpy as np
import numpy.typing as npt


class FireHazardEnum(StrEnum):
    SMOKE = "smoke"
    HAZARD_FIRE = "hazard_fire"
    SAFE_FIRE = "safe_fire"
    UNSURE_FIRE = "unsure_fire"


@dataclass
class FireHazard:
    type: FireHazardEnum
    bbox: npt.NDArray[np.float32]
