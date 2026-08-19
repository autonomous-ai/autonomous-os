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
import os
import threading
import time
from typing import Any, Optional

import hal.config as config

logger = logging.getLogger(__name__)

_stop = threading.Event()
_thread: Optional[threading.Thread] = None


# Must begin with "sensing_" to be servable by the monitor snapshot route.
SNAPSHOT_CATEGORY: str = "sensing_bearing"


def _snapshot_dir() -> str:
    root = getattr(config, "SNAPSHOT_PERSIST_DIR", "/var/lib/hal/snapshots")
    return os.path.join(root, SNAPSHOT_CATEGORY)


def _prune(directory: str) -> None:
    """Keep the newest N. Names are timestamped, so lexical order is time order."""
    keep = int(getattr(config, "BEARING_SNAPSHOT_KEEP", 30) or 0)
    if keep <= 0:
        return
    try:
        files = sorted(
            (f for f in os.listdir(directory) if f.endswith(".jpg")), reverse=True
        )
        for stale in files[keep:]:
            try:
                os.unlink(os.path.join(directory, stale))
            except OSError:
                pass
    except Exception as e:
        logger.debug("[bearing-sample] prune skipped: %s", e)


def _save_snapshot(frame: Any, box: Any, label: str) -> Optional[str]:
    """Write what this sample saw, annotated. Never raises."""
    if not getattr(config, "BEARING_SNAPSHOT_ENABLED", True) or frame is None:
        return None
    try:
        from hal.drivers.tracking.look_debug import encode_annotated

        jpg = encode_annotated(frame, box, label)
        if jpg is None:
            return None
        directory = _snapshot_dir()
        os.makedirs(directory, exist_ok=True)
        # Sorts chronologically, which is what _prune relies on.
        name = time.strftime("%Y%m%d-%H%M%S") + ".jpg"
        path = os.path.join(directory, name)
        with open(path, "wb") as f:
            f.write(jpg)
        _prune(directory)
        return path
    except Exception as e:
        logger.debug("[bearing-sample] snapshot skipped: %s", e)
        return None


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
        rejected = None  # kept only so the snapshot can show what was dismissed
        for target in ("person", "face"):
            try:
                found = detector.detect(frame, target, strict=False)
            except Exception:
                continue
            if found is None:
                continue
            if aim._is_near_enough(found, frame, target):
                box, kind = found, target
                break
            if rejected is None:
                rejected = (found, target)
    finally:
        aim._detector_lock_use.release()

    frame_h, frame_w = float(frame.shape[0]), float(frame.shape[1])

    if box is None:
        if rejected is not None:
            far_box, far_kind = rejected
            _save_snapshot(
                frame, far_box,
                f"ignored: far {far_kind} h={far_box[3] / frame_h * 100:.1f}%",
            )
        return False

    x, y, w, h = box
    dx_frac = ((x + w / 2.0) - frame_w / 2.0) / frame_w
    dy_frac = ((y + h / 2.0) - frame_h / 2.0) / frame_h
    if abs(dx_frac) > config.BEARING_SAMPLE_MAX_DX_FRAC:
        _save_snapshot(frame, box, f"skipped: dx={dx_frac * 100:+.1f}% too far off centre")
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

    recorded = user_bearing.record_sighting(bearing, pose=pose)
    note = "" if pose is not None else " (bearing only, not vertically centred)"
    if recorded:
        _save_snapshot(
            frame, box,
            f"recorded {kind} dx={dx_frac * 100:+.1f}% -> bearing {bearing:+.1f}{note}",
        )
        logger.info(
            "[bearing-sample] near %s at dx=%+.1f%% dy=%+.1f%% -> bearing %+.1f%s",
            kind, dx_frac * 100.0, dy_frac * 100.0, bearing, note,
        )
        return True
    # Rejected by the estimate's own rate limit — still worth a picture.
    _save_snapshot(frame, box, f"not folded in (too soon) {kind} dx={dx_frac * 100:+.1f}%")
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
