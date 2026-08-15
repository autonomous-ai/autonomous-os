"""Board platform layer — the single source of truth for per-board wiring.

Consolidates device-tree detection and per-board pin/transport config that was
previously duplicated across rgb_service, gpio_button, and ttp223 (each opened
/proc/device-tree/model and re-implemented its own `_is_*` checks).

Drivers ask `board_profile()` for wiring; they never re-detect the board. This
is the Autonomous equivalent of the Linux arch/ layer: generic driver code sits
above, board-specific values live as DATA in `boards.json` (next to this module,
the DTS / Android BoardConfig analogue), and a new board is one JSON entry — no
code change. This module just loads that data into typed structs and classifies.

Pure + testable: `detect_board_id()` takes an optional model string, so the whole
classification can be unit-tested with no hardware and no /proc.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

DEVICE_TREE_MODEL_PATH = "/proc/device-tree/model"
BOARDS_DATA_PATH = os.path.join(os.path.dirname(__file__), "boards.json")

logger = logging.getLogger("hal.board")


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


# --- per-board wiring: loaded from boards.json (data, not code) -------------
# A new board is a JSON entry; this module never hardcodes board values. The
# data file ships inside the HAL package, so a missing/invalid one is a
# packaging fault — fail loudly rather than guess wiring (see DEVICE-SPEC rule #3).


def _load_boards(
    path: str = BOARDS_DATA_PATH,
) -> Tuple[Dict[str, BoardProfile], List[Tuple[List[str], str]], str]:
    """Parse boards.json → (profiles, matchers, default_board_id). Pure given a path.

    matchers preserve file order; each is (lowercased model substrings, board_id).
    """
    with open(path, "r") as f:
        data = json.load(f)
    profiles: Dict[str, BoardProfile] = {}
    matchers: List[Tuple[List[str], str]] = []
    for bid, b in data["boards"].items():
        touch = b.get("touch")
        profiles[bid] = BoardProfile(
            id=bid,
            led=LedConfig(**b["led"]),
            button=ButtonConfig(**b["button"]),
            touch=TouchConfig(**touch) if touch else None,
        )
        matchers.append(([s.lower() for s in b.get("match", [])], bid))
    return profiles, matchers, data["default_board"]


try:
    PROFILES, _MATCHERS, DEFAULT_BOARD_ID = _load_boards()
except (OSError, ValueError, KeyError, TypeError) as e:
    raise RuntimeError(
        f"Board profile data invalid or missing at {BOARDS_DATA_PATH}: {e}"
    ) from e


# HAL_BOARD forces a board id instead of reading /proc/device-tree/model. It
# exists for the mock body (devices/sim): a laptop has no device tree, so no
# board can be detected and HAL refuses to boot. It is opt-in, must name a real
# boards.json entry, and is logged loudly — a robot never sets it.
BOARD_ENV_VAR = "HAL_BOARD"


def board_override() -> Optional[str]:
    """The board id forced by HAL_BOARD, or None. Unknown ids are refused."""
    forced = os.environ.get(BOARD_ENV_VAR, "").strip()
    if not forced:
        return None
    if forced not in PROFILES:
        raise RuntimeError(
            f"{BOARD_ENV_VAR}={forced!r} is not a board in boards.json "
            f"(known: {sorted(PROFILES)}). Refusing to boot on an invented board."
        )
    return forced


def matched_board_id(model: Optional[str] = None) -> Optional[str]:
    """The board whose `match` substrings appear in the device-tree model, or
    None if the model matches no known board. Pure; testable.

    Unlike detect_board_id this does NOT fall back to `default_board`: the
    board-support gate must tell a genuine hardware match from a blind default,
    so it needs to see the None.
    """
    if model is None:
        if forced := board_override():
            return forced
    m = model if model is not None else read_device_tree_model()
    for substrings, bid in _MATCHERS:
        if any(s in m for s in substrings):
            return bid
    return None


def detect_board_id(model: Optional[str] = None) -> str:
    """Classify the board from the device-tree model string. Pure; testable.

    Tests the lowercased `match` substrings from boards.json (first hit wins, in
    file order — keep them non-overlapping). Unrecognized/empty model falls back
    to `default_board`. e.g. 'pi 5'→Pi 5, 'pi 4'→Pi 4, 'sun60iw2'→OrangePi 4 Pro.
    """
    return matched_board_id(model) or DEFAULT_BOARD_ID


def assert_board_supported(declared: List[str], model: Optional[str] = None) -> str:
    """Fail loud unless the physical board is one this device declares in
    DEVICE.md `boards`. Returns the resolved board id.

    Wrong hardware means wrong pin maps; actuating servos/LEDs against an
    unverified board is a hardware fault, not graceful degradation
    (DEVICE-SPEC rule #3). Two ways to abort:
      - the model matches no boards.json entry → unidentifiable hardware, no
        wiring profile can be trusted;
      - it matches a real board the device does not declare → unsupported.
    """
    if model is None:
        if forced := board_override():
            if declared and forced not in declared:
                raise RuntimeError(
                    f"{BOARD_ENV_VAR}={forced!r} but this device declares boards "
                    f"{declared}. Refusing to boot — declare the board or drop the override."
                )
            logger.warning("[board] %s=%s — board detection skipped (mock body / off-device run)",
                           BOARD_ENV_VAR, forced)
            return forced
    m = model if model is not None else read_device_tree_model()
    matched = matched_board_id(m)
    if matched is None:
        raise RuntimeError(
            f"Unknown board: device-tree model {m!r} matches no entry in boards.json "
            f"(device declares boards {declared}). Refusing to boot on unidentified "
            f"hardware — wiring is unverifiable."
        )
    if declared and matched not in declared:
        raise RuntimeError(
            f"Board '{matched}' is not supported by this device (DEVICE.md boards: "
            f"{declared}). Refusing to boot — pin maps would be wrong."
        )
    return matched


@lru_cache(maxsize=1)
def board_profile() -> BoardProfile:
    """The active board's profile (cached). Drivers call this, not /proc."""
    return PROFILES[detect_board_id()]
