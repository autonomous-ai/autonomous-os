"""Device-owned SEN55 wiring; no bus or header pins are assumed."""

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SEN55Config:
    bus: int

    def __post_init__(self):
        if type(self.bus) is not int or self.bus < 0:
            raise ValueError("bus must be a nonnegative integer")


def load_sen55_config(device_dir: str, board_id: str) -> SEN55Config | None:
    """Load and validate the complete sen55.json boards map, like MPR121."""
    path = Path(device_dir) / "sen55.json"
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or set(data) != {"boards"} or not isinstance(data["boards"], dict):
            raise ValueError("expected an object containing a 'boards' map")
        configs = {}
        for board, entry in data["boards"].items():
            if not isinstance(entry, dict) or set(entry) - {"enabled", "bus"}:
                raise ValueError(f"{board}: invalid SEN55 configuration fields")
            enabled = entry.get("enabled", True)
            if type(enabled) is not bool:
                raise ValueError(f"{board}: enabled must be a boolean")
            # Validate any provided wiring even when disabled, but do not
            # require a fabricated bus for hardware not wired yet.
            config = SEN55Config(entry["bus"]) if "bus" in entry else None
            if enabled and config is None:
                raise ValueError(f"{board}: enabled SEN55 requires bus")
            configs[board] = config if enabled else None
        return configs.get(board_id)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid SEN55 wiring at {path}: {exc}") from exc
