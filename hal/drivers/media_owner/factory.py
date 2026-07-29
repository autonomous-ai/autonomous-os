"""Media owner factory — resolve a DEVICE.md `owner:` name to a handover class.

Mirrors hal/drivers/motors/factory.py and hal/drivers/camera/factory.py. Those
answer "which implementation opens this hardware"; this one answers "who has to
let go of it first". Same rule in all three: the device declares a name, the
registry maps it to a class, and nothing outside this table knows which body is
running.

Behavior:
  - owner absent (None)           → None (HAL owns the hardware; the normal case)
  - owner known + importable      → return the class
  - owner known + ImportError     → return None + warning (HAL boots degraded)
  - owner unknown                 → RuntimeError (deploy fault, fail loud)

Unknown always fails loud, with no required/optional split: a capability naming
an owner that does not exist cannot be honoured, and carrying on means opening
hardware someone else still holds — a "device busy" failure much further away
from its cause.
"""
from __future__ import annotations

import importlib
import logging
from typing import Optional, Tuple

logger = logging.getLogger("hal.media_owner.factory")

# Registry: owner name → (module_path, class_name)
MEDIA_OWNERS: dict[str, Tuple[str, str]] = {
    # Pollen Robotics' reachy_mini daemon (Reachy Mini). Holds /dev/video* and
    # both ALSA PCMs for its own app runtime; keeps running, and keeps driving
    # the motors, after handing the media over.
    "pollen_daemon": ("hal.drivers.media_owner.pollen", "PollenDaemonMediaOwner"),
}


def resolve_media_owner(owner: Optional[str]) -> Optional[type]:
    """Resolve an `owner:` name to its handover class.

    Args:
        owner: the `owner:` value from a DEVICE.md capability, or None.

    Returns:
        The handover class, or None when no owner is declared or its module
        could not be imported. Raises on an unknown name.
    """
    if owner is None:
        return None

    entry = MEDIA_OWNERS.get(owner)
    if entry is None:
        registered = sorted(MEDIA_OWNERS.keys())
        raise RuntimeError(
            f"DEVICE.md declares media owner '{owner}' but no handover is "
            f"registered for it (registered: {registered}). Implement it and "
            f"register in hal/drivers/media_owner/factory.py, or fix the name."
        )

    module_path, class_name = entry
    try:
        mod = importlib.import_module(module_path)
        return getattr(mod, class_name)
    except (ImportError, AttributeError) as e:
        logger.warning(
            "[media-owner] '%s' registered but import failed: %s — HAL will open "
            "the hardware without asking, and it will likely report busy",
            owner, e,
        )
        return None
