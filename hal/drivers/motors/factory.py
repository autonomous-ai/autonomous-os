"""Motion driver factory — resolve a ROBOT.md `driver:` name to a service class.

The selector covers motion only (light/ws2812 stays informational — YAGNI).
When a new motion backend is implemented, register it here.

Behavior:
  - driver absent (None)          → default "feetech" + warning (schema v1 compat)
  - driver known + importable     → return the class
  - driver known + ImportError    → return None (plan_mounts handles required/optional)
  - driver unknown + required     → RuntimeError (deploy fault, fail loud)
  - driver unknown + optional     → return None + warning
"""
from __future__ import annotations

import importlib
import logging
from typing import Optional, Tuple

logger = logging.getLogger("hal.motion.factory")

# Registry: driver name → (module_path, class_name)
# Add new motion backends here as they are implemented.
MOTION_DRIVERS: dict[str, Tuple[str, str]] = {
    "feetech": ("hal.drivers.motors.animation_service", "AnimationService"),
    "reachy_sdk": ("hal.drivers.motors.reachy_service", "ReachyMotionService"),
}


def resolve_motion_class(driver: Optional[str], required: bool) -> Optional[type]:
    """Resolve a motion driver name to its service class.

    Args:
        driver: the `driver:` value from ROBOT.md motion capability, or None.
        required: whether the motion capability is declared required.

    Returns:
        The motion service class, or None when the driver is unavailable/unknown
        and the capability is optional. Raises on unknown + required.
    """
    if driver is None:
        logger.warning(
            "[motion] no driver: declared in ROBOT.md motion capability, "
            "defaulting to 'feetech' (explicit driver will be required in schema v2)"
        )
        driver = "feetech"

    entry = MOTION_DRIVERS.get(driver)
    if entry is None:
        registered = sorted(MOTION_DRIVERS.keys())
        if required:
            raise RuntimeError(
                f"ROBOT.md declares motion driver '{driver}' but no motion "
                f"service is registered for it (registered: {registered}). "
                f"Implement it and register in hal/drivers/motors/factory.py, "
                f"or fix the driver name."
            )
        logger.warning(
            "[motion] driver '%s' is not registered (registered: %s); "
            "motion capability is optional, skipping",
            driver, registered,
        )
        return None

    module_path, class_name = entry
    try:
        mod = importlib.import_module(module_path)
        return getattr(mod, class_name)
    except (ImportError, AttributeError) as e:
        logger.warning("[motion] driver '%s' registered but import failed: %s", driver, e)
        return None
