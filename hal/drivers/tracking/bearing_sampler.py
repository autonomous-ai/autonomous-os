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
  * Only a FACE is learned from, never a `person` box. A body fills the frame
    whenever the camera is aimed low, so learning from one memorises the pose
    that was pointing at the desk and calls it "where the user is". A face in
    frame proves the opposite by construction: this posture sees a head, so
    restoring it will see one again.

The POSTURE is recorded wherever in frame the face sat. The vertical gate that
once guarded it was written for person boxes, where a centred torso said nothing
about whether the head was in frame; keeping it for faces was self-defeating,
because while the camera is aimed low every face sits near the top edge, so
every sighting failed the gate and nothing was ever stored to restore.
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


def _conf_txt(conf: Any) -> str:
    return f" conf={conf:.2f}" if isinstance(conf, (int, float)) else ""


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
        conf = None
        rejected = None  # kept only so the snapshot can show what was dismissed
        # FACE only, never `person`. A person box says where a body is, and a
        # body fills the frame whenever the camera is aimed low — so learning
        # from it memorises the pose that was pointing at the desk and calls it
        # "where the user is". Device-observed: 22 samples, confidence 0.99, and
        # a stored posture with wrist_pitch -78 that could not see a face at all.
        # A face in frame proves the opposite by construction: this pose sees a
        # head, so restoring it will see one again.
        for target in ("face",):
            try:
                found = detector.detect(
                    frame, target, strict=False,
                    min_conf=config.LOOK_AIM_MIN_CONFIDENCE,
                )
            except TypeError:
                found = detector.detect(frame, target, strict=False)
            except Exception:
                continue
            if found is None:
                continue
            if aim._is_near_enough(found, frame, target):
                box, kind = found, target
                conf = getattr(detector, "last_confidence", None)
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
    # Posture is recorded whenever a FACE was the thing seen, wherever it sat in
    # frame. The vertical gate that used to stand here was written for `person`
    # boxes, where a centred torso said nothing about whether the head was in
    # frame — a posture learned from one could be aimed at a chest. A face
    # carries its own proof: this posture sees a head.
    #
    # Keeping the gate for faces was self-defeating. While the camera is aimed
    # low every face sits near the top edge, so every sighting failed the gate,
    # so no posture was ever stored, so there was nothing to restore and the
    # camera stayed low — device-observed dy of -15.8% then -41.2%, two
    # sightings, `joints=1`, a remembered "pose" containing only a yaw. A
    # posture that catches the user at the frame edge is imperfect; it is also
    # incomparably better than one pointing at the desk, and the estimate's EMA
    # walks it toward centre as the framing it enables improves.
    pose = dict(positions)
    pose["base_yaw.pos"] = bearing

    recorded = user_bearing.record_sighting(bearing, pose=pose)
    if recorded:
        # dy rides along in the caption because how far off centre the face sat
        # is what tells a reader whether this posture is a good one — the log
        # line below already carries it, the picture did not.
        _save_snapshot(
            frame, box,
            f"recorded {kind}{_conf_txt(conf)} dx={dx_frac * 100:+.1f}% "
            f"dy={dy_frac * 100:+.1f}% -> bearing {bearing:+.1f}",
        )
        logger.info(
            "[bearing-sample] near %s at dx=%+.1f%% dy=%+.1f%% -> bearing %+.1f",
            kind, dx_frac * 100.0, dy_frac * 100.0, bearing,
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


def sample_now() -> bool:
    """Take a sighting immediately, instead of waiting for the next tick.

    For callers who have just made the arm point at somebody and would otherwise
    throw that away — a search that stopped on a subject knows WHERE it stopped,
    but not whether they are centred, and record_sighting cannot be given an
    off-centre yaw without silently biasing the estimate. So the honest move is
    to let the sampler look for itself: it already checks framing, privacy and
    whether the body is free.

    Returns whether a sighting was actually recorded.
    """
    try:
        return _sample_once()
    except Exception as e:
        logger.debug("[bearing-sample] on-demand sample skipped: %s", e)
        return False


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
