"""Load mechanical button wiring owned by a device, keyed by board ID."""

import json
from pathlib import Path

from hal.board.board import ButtonConfig, PROFILES


def load_button_config(device_dir: str, board_id: str) -> ButtonConfig:
    """Absent file/board uses legacy board wiring; invalid declarations are boot errors.

    GPIO lines are chip-relative offsets, not physical header pin numbers.
    The shared driver uses pull-up and active-low wiring.
    """
    fallback = PROFILES[board_id].button
    path = Path(device_dir) / "gpio_button.json"
    try:
        text = path.read_text()
    except FileNotFoundError:
        return fallback
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or set(data) != {"boards"}:
            raise ValueError("expected an object containing only 'boards'")
        boards = data["boards"]
        if not isinstance(boards, dict):
            raise ValueError("'boards' must be an object")
        configs = {}
        for name, values in boards.items():
            if not isinstance(values, dict) or set(values) != {"chip", "line", "debounce_ns"}:
                raise ValueError(f"{name}: expected chip, line and debounce_ns")
            if any(type(value) is not int or value < 0 for value in values.values()):
                raise ValueError(f"{name}: wiring values must be non-negative integers")
            configs[name] = ButtonConfig(**values)
        return configs.get(board_id, fallback)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid GPIO button wiring at {path}: {exc}") from exc
