"""Passive learning of where the user usually is.

The remembered bearing used to learn only from `look` questions that happened to
end near-perfectly centred: two samples in a full day of device testing, against
a six-hour confidence half-life. It decayed faster than it learned, so the one
thing that could rescue a look when nobody is visible was never confident enough
to be consulted.

This watches, on a slow cadence, for a person close enough to plausibly be the
user and folds that sighting in. It never moves the lamp — it only reads the
camera and the servo positions, so it cannot interrupt anything.

Two accuracy rules, both about not teaching the lamp something false:

  * The bearing tolerates a horizontal offset, recovered as `yaw + dx x scale`,
    but only a bounded one — that correction leans on the very FOV constant the
    aim was rewritten to stop trusting.
  * The POSTURE is recorded only when the subject is vertically centred too.
    Pitch cannot be corrected arithmetically here, so a subject high or low in
    frame means the current pitch is NOT looking at them, and storing it would
    teach a posture aimed at the floor.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

import hal.config as config

logger = logging.getLogger(__name__)

_stop = threading.Event()
_thread: Optional[threading.Thread] = None


def _sample_once() -> bool:
    """One observation. Returns True when a sighting was recorded."""
    import hal.app_state as state

    from hal.drivers.tracking import aim, user_bearing

    if getattr(state, "_camera_disabled", False):
        return False  # privacy: never watch someone who asked us not to
    cap = getattr(state, "camera_capture", None)
    svc = getattr(state, "animation_service", None)
    if cap is None or svc is None:
        return False
    # The body is busy aiming or tracking; its pose is mid-flight and means
    # nothing, and the detector is in use.
    if getattr(svc, "_tracking_active", False):
        return False

    detector = aim.get_detector()
    if detector is None:
        return False

    # Non-blocking: a look in progress must never wait on a background sample.
    if not aim._detector_lock_use.acquire(blocking=False):
        return False
    try:
        with aim._camera_consumer(cap):
            frame = aim._grab_frame(cap, svc)
        if frame is None:
            return False
        box = None
        kind = ""
        for target in ("person", "face"):
            try:
                found = detector.detect(frame, target, strict=False)
            except Exception:
                continue
            if found is not None and aim._is_near_enough(found, frame, target):
                box, kind = found, target
                break
    finally:
        aim._detector_lock_use.release()

    if box is None:
        return False

    x, y, w, h = box
    frame_h, frame_w = float(frame.shape[0]), float(frame.shape[1])
    dx_frac = ((x + w / 2.0) - frame_w / 2.0) / frame_w
    dy_frac = ((y + h / 2.0) - frame_h / 2.0) / frame_h
    if abs(dx_frac) > config.BEARING_SAMPLE_MAX_DX_FRAC:
        return False

    try:
        positions = svc.get_positions()
        yaw = float(positions.get("base_yaw.pos", 0.0))
    except Exception:
        return False

    bearing = yaw + dx_frac * float(config.LOOK_AIM_FOV_DEG)
    # Posture only when it genuinely points at them — see the module docstring.
    pose = positions if abs(dy_frac) <= config.BEARING_SAMPLE_MAX_DY_FRAC else None
    if pose is not None:
        pose = dict(pose)
        pose["base_yaw.pos"] = bearing

    if user_bearing.record_sighting(bearing, pose=pose):
        logger.info(
            "[bearing-sample] near %s at dx=%+.1f%% dy=%+.1f%% -> bearing %+.1f%s",
            kind, dx_frac * 100.0, dy_frac * 100.0, bearing,
            "" if pose is not None else " (bearing only, not vertically centred)",
        )
        return True
    return False


def _loop() -> None:
    interval = max(30.0, float(config.BEARING_SAMPLE_INTERVAL_S))
    # Offset the first sample so it does not land in the middle of start-up,
    # when the camera and detector are still warming.
    if _stop.wait(min(60.0, interval)):
        return
    while not _stop.is_set():
        try:
            _sample_once()
        except Exception as e:  # a background learner must never take HAL down
            logger.debug("[bearing-sample] skipped: %s", e)
        if _stop.wait(interval):
            return


def start() -> None:
    """Begin passive sampling. Idempotent."""
    global _thread
    if not config.BEARING_SAMPLE_ENABLED:
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="bearing-sampler")
    _thread.start()
    logger.info(
        "[bearing-sample] watching every %.0fs for a nearby person",
        config.BEARING_SAMPLE_INTERVAL_S,
    )


def stop() -> None:
    _stop.set()
