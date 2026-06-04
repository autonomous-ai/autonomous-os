"""Board platform layer — the single source of truth for per-board wiring.

Consolidates device-tree detection and per-board pin/transport config that was
previously duplicated across rgb_service, gpio_button, and ttp223 (each opened
/proc/device-tree/model and re-implemented its own `_is_*` checks).

Drivers ask `board_profile()` for wiring; they never re-detect the board. This
is the Autonomous equivalent of the Linux arch/ layer: generic driver code sits
above, board-specific values live here, and a new board is one new entry.

Pure + testable: `detect_board_id()` takes an optional model string, so the whole
classification can be unit-tested with no hardware and no /proc.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, List, Optional

DEVICE_TREE_MODEL_PATH = "/proc/device-tree/model"


def read_device_tree_model(path: str = DEVICE_TREE_MODEL_PATH) -> str:
    """Lower-cased /proc/device-tree/model contents, or '' if unavailable."""
    try:
        with open(path, "r") as f:
            return f.read().rstrip("\x00").strip().lower()
    except OSError:
        return ""


@dataclass(frozen=True)
class LedConfig:
    transport: str          # "pwm" (Pi 4) | "spi" (Pi 5, OrangePi)
    spi_bus: int = 0
    spi_device: int = 0
    pwm_pin: int = 12


@dataclass(frozen=True)
class ButtonConfig:
    chip: int
    line: int
    debounce_ns: int


@dataclass(frozen=True)
class TouchConfig:
    chip: int
    lines: List[int]


@dataclass(frozen=True)
class BoardProfile:
    id: str
    led: LedConfig
    button: ButtonConfig
    touch: Optional[TouchConfig] = None


# --- per-board wiring (was scattered as module constants across drivers) ---

_PI_BUTTON = ButtonConfig(chip=0, line=17, debounce_ns=200_000_000)

PROFILES: Dict[str, BoardProfile] = {
    "raspberry_pi_5": BoardProfile(
        id="raspberry_pi_5",
        led=LedConfig(transport="spi", spi_bus=0, spi_device=0),
        button=_PI_BUTTON,
    ),
    "raspberry_pi_4": BoardProfile(
        id="raspberry_pi_4",
        led=LedConfig(transport="pwm", pwm_pin=12),
        button=_PI_BUTTON,
    ),
    "orangepi_sun60": BoardProfile(
        id="orangepi_sun60",
        led=LedConfig(transport="spi", spi_bus=3, spi_device=0),
        button=ButtonConfig(chip=1, line=9, debounce_ns=200_000_000),
        touch=TouchConfig(chip=0, lines=[96, 97, 98, 99]),
    ),
}

# Most conservative fallback when the model string is unknown: Pi 4 wiring
# (PWM LED on GPIO 12, button on gpiochip0 line 17). Matches the pre-refactor
# `else` branch in each driver.
DEFAULT_BOARD_ID = "raspberry_pi_4"


def detect_board_id(model: Optional[str] = None) -> str:
    """Classify the board from the device-tree model string. Pure; testable.

    Mirrors the exact substring checks the drivers used before consolidation:
    'pi 5' -> Pi 5, 'sun60iw2' -> OrangePi sun60, else the conservative default.
    """
    m = model if model is not None else read_device_tree_model()
    if "pi 5" in m:
        return "raspberry_pi_5"
    if "sun60iw2" in m:
        return "orangepi_sun60"
    return DEFAULT_BOARD_ID


@lru_cache(maxsize=1)
def board_profile() -> BoardProfile:
    """The active board's profile (cached). Drivers call this, not /proc."""
    return PROFILES[detect_board_id()]
