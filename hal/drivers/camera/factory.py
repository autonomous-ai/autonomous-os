"""Camera driver factory — resolve a DEVICE.md `driver:` name to a device class.

Mirrors hal/drivers/motors/factory.py. The camera is the second capability where
one contract has genuinely different hardware paths behind it: Lamp's UVC webcam
is opened by OpenCV over V4L2, while Reachy Mini's CSI sensor lives behind
libcamera and cannot be read that way at all (the raw Bayer node hands OpenCV
timeouts forever). Both satisfy `VideoCaptureDeviceBase`, so routes, sensing and
the tracker stay hardware-neutral; only this table knows which one to build.

Behavior:
  - driver absent (None)          → default "opencv" (schema v1 compat)
  - driver known + importable     → return the class
  - driver known + ImportError    → return None (caller degrades / fails loud)
  - driver unknown + required     → RuntimeError (deploy fault, fail loud)
  - driver unknown + optional     → return None + warning
"""
from __future__ import annotations

import importlib
import logging
from typing import Optional, Tuple

logger = logging.getLogger("hal.camera.factory")

# Registry: driver name → (module_path, class_name)
# Add new camera backends here as they are implemented.
CAMERA_DRIVERS: dict[str, Tuple[str, str]] = {
    # UVC / V4L2 webcams read through OpenCV. Carries the USB-specific healing
    # (ISP freeze watchdog, colour-corruption recovery, USB power-cycle).
    "opencv": ("hal.drivers.camera.video_capture_device", "LocalVideoCaptureDevice"),
    # Raspberry Pi CSI sensors behind libcamera, via an rpicam-vid MJPEG pipe.
    "rpicam": ("hal.drivers.camera.rpicam_capture_device", "RpicamVideoCaptureDevice"),
}


def resolve_camera_class(driver: Optional[str], required: bool) -> Optional[type]:
    """Resolve a camera driver name to its capture device class.

    Args:
        driver: the `driver:` value from DEVICE.md vision capability, or None.
        required: whether the vision capability is declared required.

    Returns:
        The capture device class, or None when the driver is unavailable/unknown
        and the capability is optional. Raises on unknown + required.
    """
    if driver is None:
        # Not a warning: every device that predates this selector is a UVC
        # webcam, and saying so on each boot would be noise.
        driver = "opencv"

    entry = CAMERA_DRIVERS.get(driver)
    if entry is None:
        registered = sorted(CAMERA_DRIVERS.keys())
        if required:
            raise RuntimeError(
                f"DEVICE.md declares camera driver '{driver}' but no capture "
                f"device is registered for it (registered: {registered}). "
                f"Implement it and register in hal/drivers/camera/factory.py, "
                f"or fix the driver name."
            )
        logger.warning(
            "[camera] driver '%s' is not registered (registered: %s); "
            "vision capability is optional, skipping",
            driver, registered,
        )
        return None

    module_path, class_name = entry
    try:
        mod = importlib.import_module(module_path)
        return getattr(mod, class_name)
    except (ImportError, AttributeError) as e:
        logger.warning("[camera] driver '%s' registered but import failed: %s", driver, e)
        return None
