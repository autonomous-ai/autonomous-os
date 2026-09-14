"""Device-owned SEN63C wiring and acquisition options."""

import json
from dataclasses import dataclass, fields
from pathlib import Path

from hal.board.sen55 import SEN55Timing, validate_header_pins


@dataclass(frozen=True)
class SEN63CTiming(SEN55Timing):
    poll_interval_s: float = 1.0
    stale_after_s: float = 5.0


@dataclass(frozen=True)
class SEN63CConfig:
    bus: int
    timing: SEN63CTiming = SEN63CTiming()
    sda_pin: int | None = None
    scl_pin: int | None = None
    automatic_self_calibration: bool | None = None

    def __post_init__(self):
        validate_header_pins(self.sda_pin, self.scl_pin)
        if type(self.bus) is not int or self.bus < 0:
            raise ValueError("bus must be a nonnegative integer")
        validate_calibration(self.automatic_self_calibration)


def validate_calibration(value):
    if value is not None and type(value) is not bool:
        raise ValueError("automatic_self_calibration must be a boolean or null")


def load_sen63c_config(device_dir: str, board_id: str) -> SEN63CConfig | None:
    """Validate all board entries, including disabled hardware declarations."""
    path = Path(device_dir) / "sen63c.json"
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or set(data) != {"boards"} or not isinstance(data["boards"], dict):
            raise ValueError("expected an object containing a 'boards' map")
        configs = {}
        allowed = {"enabled", "bus", "sda_pin", "scl_pin", "automatic_self_calibration"} | {f.name for f in fields(SEN63CTiming)}
        for board, entry in data["boards"].items():
            if not isinstance(entry, dict) or set(entry) - allowed:
                raise ValueError(f"{board}: invalid SEN63C configuration fields")
            enabled = entry.get("enabled", True)
            if type(enabled) is not bool:
                raise ValueError(f"{board}: enabled must be a boolean")
            timing = SEN63CTiming(**{
                f.name: entry[f.name] for f in fields(SEN63CTiming) if f.name in entry
            })
            sda_pin, scl_pin = entry.get("sda_pin"), entry.get("scl_pin")
            validate_header_pins(sda_pin, scl_pin)
            calibration = entry.get("automatic_self_calibration")
            validate_calibration(calibration)
            config = SEN63CConfig(entry["bus"], timing, sda_pin, scl_pin, calibration) if entry.get("bus") is not None else None
            if enabled and config is None:
                raise ValueError(f"{board}: enabled SEN63C requires bus")
            configs[board] = config if enabled else None
        return configs.get(board_id)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid SEN63C wiring at {path}: {exc}") from exc
