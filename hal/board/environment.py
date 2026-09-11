"""Device-owned component selection for the shared environment capability."""

import json
from pathlib import Path

SUPPORTED_COMPONENTS = ("sen55", "scd41")


def load_environment_components(device_dir: str) -> tuple[str, ...]:
    """Older profiles without a component list retain their SEN55 backend."""
    path = Path(device_dir) / "environment.json"
    try:
        text = path.read_text()
    except FileNotFoundError:
        return ("sen55",)
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or set(data) != {"components"}:
            raise ValueError("expected an object containing components")
        components = data["components"]
        if not isinstance(components, list) or any(
            not isinstance(name, str) or name not in SUPPORTED_COMPONENTS
            for name in components
        ):
            raise ValueError(f"components must be a list drawn from {SUPPORTED_COMPONENTS}")
        if len(set(components)) != len(components):
            raise ValueError("duplicate environment components")
        return tuple(components)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid environment components at {path}: {exc}") from exc
