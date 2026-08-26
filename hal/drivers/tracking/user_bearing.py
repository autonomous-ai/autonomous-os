"""Remember roughly where the user is, as a single servo yaw.

The lamp only ever needs ONE angle to turn to, so this deliberately stores one
estimate rather than a per-user, per-hour histogram. When the user is
visible the offset is computed live and this is not consulted at all; it exists
for the moments they are NOT visible.

Where it earns its keep, in order:
  1. the idle resting pose — pointing the camera somewhere sensible in advance,
     so a visual question usually finds the user already in frame. This is the
     main consumer, and it works by making the fallback unnecessary.
  2. the bearing fallback, when a `look` fires with nothing in frame.
  3. seeding a search sweep.

Only CENTRED sightings are recorded, which is what removes the camera-FOV
dependency: with the face within a couple of percent of frame centre, the servo
position IS the bearing, so there is no pixel->angle conversion for a wrong FOV
or a wrong projection model to corrupt (this camera's FOV constant is disputed —
60 deg in constants.py vs 78 deg in the hardware BOM — and it shows visible
barrel distortion).

Angles are NOT averaged circularly. base_yaw is a bounded servo range
(-135..+135) that does not wrap, so a plain linear mean is correct here; a
circular mean would be wrong at the extremes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

import hal.config as config

logger = logging.getLogger(__name__)

SCHEMA_VERSION: int = 3
# Which component of the pose IS the bearing direction.
BASE_YAW_JOINT: str = "base_yaw.pos"


def _calibration_path() -> Optional[str]:
    """The calibration file the arm actually loaded, or None.

    ASK the robot rather than recomputing. lerobot resolves this once, in
    Robot.__init__, and keeps it on `calibration_fpath` — the file it really
    read. Deriving it a second time from DEVICE_ID and the two candidate
    directories means two copies of a rule that can drift, and a mirror that
    drifts would fingerprint a file the arm never loaded: either dropping good
    bearings on every read, or accepting a stale pose from the wrong unit.

    The derivation below is kept only as a fallback for when there is no live
    robot to ask — off-device tests, and the window before the arm connects.
    """
    try:
        import hal.app_state as state

        svc = getattr(state, "animation_service", None)
        fpath = getattr(getattr(svc, "robot", None), "calibration_fpath", None)
        if fpath:
            return str(fpath)
    except Exception:
        pass
    try:
        from hal.config import DEVICE_ID
        from hal.follower.config_hal_follower import (
            CALIBRATION_DIR,
            PERSISTENT_CALIBRATION_DIR,
        )
    except Exception:
        return None
    try:
        if DEVICE_ID and DEVICE_ID != "hal":
            per_device = PERSISTENT_CALIBRATION_DIR / f"{DEVICE_ID}.json"
            if per_device.is_file():
                return str(per_device)
        return str(CALIBRATION_DIR / "hal.json")
    except Exception:
        return None


def _calibration_fingerprint() -> Optional[str]:
    """Short content hash of the live calibration, or None if unreadable.

    CONTENT, not mtime: an OTA rewrites the file's timestamp without changing a
    single offset, and invalidating a good bearing on every update would be its
    own bug.
    """
    path = _calibration_path()
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except Exception:
        return None

# Weight of each new sighting in the running mean. Low enough that one stray
# sample cannot move the estimate far, high enough to follow a real move within
# a handful of sightings.
EMA_ALPHA: float = 0.25
# Ignore sightings closer together than this: someone sitting still for an hour
# must not drown out every other position they use.
MIN_SAMPLE_INTERVAL_S: float = 30.0
# A sighting this far from a settled estimate is treated as suspect — most
# likely someone walking past, not the user changing seat. Damped instead of
# rejected, so a REAL move still wins once it repeats (see OUTLIER_STREAK).
OUTLIER_DEG: float = 45.0
OUTLIER_ALPHA: float = 0.05
# ...but this many suspect sightings in a row is not a passer-by, it is a
# relocation. Accept the new position wholesale and rebuild confidence from it.
OUTLIER_STREAK: int = 3
# Below this confidence the estimate is not settled enough to call anything an
# outlier — early sightings must be free to move it.
OUTLIER_MIN_CONFIDENCE: float = 0.4
# Prediction-failure detection — how a MOVED LAMP is noticed.
#
# The bearing is stored in lamp-relative coordinates, so picking the lamp up or
# rotating it on the desk invalidates every stored bearing at once — while the
# file still looks perfectly valid. Nothing on this device can observe that
# directly: there is no IMU, and base_yaw measures the head against the BASE, so
# rotating the whole lamp moves the world and not the joint.
#
# So it is inferred: when the aim turns to the remembered bearing and finds
# nobody, that is a failed prediction. A few in a row means the bearing no
# longer describes reality — lamp moved, furniture rearranged, or the user
# changed desk. The cause does not matter; the response is the same, and it
# self-heals because the estimate rebuilds from live sightings.
PREDICTION_MISS_LIMIT: int = 3
# ...and they must be CLUSTERED. A moved lamp fails every attempt from the moment
# it moved; a user who is occasionally in another room produces isolated misses
# spread over weeks. Without a window those look identical after enough time, and
# a perfectly good bearing gets dropped for three unrelated absences months apart.
MISS_STREAK_WINDOW_S: float = 24 * 3600.0
# Confidence reaches ~1.0 after this many sightings, and STAYS there.
#
# It deliberately does not decay with age. Confidence answers "how well is this
# estimate learned", not "how recent is it" — age is reported separately as
# `age_s` for anyone who wants it. Staleness is caught by the prediction-failure
# path instead (MISS_STREAK above): a bearing that stops working is dropped
# outright, which is a sharper and more honest signal than a number sagging on a
# timer. The old six-hour half-life also fought the sampler that feeds this
# file — an aim-only device recorded roughly two sightings a day, so the
# estimate decayed faster than it could learn and the bearing was usually
# refused for low confidence exactly when it was needed.
CONFIDENCE_FULL_SAMPLES: int = 8


@dataclass
class BearingEstimate:
    bearing_deg: float
    confidence: float
    samples: int
    updated: float          # epoch seconds
    age_s: float
    # Full remembered posture, {joint: degrees}. `bearing_deg` is the base_yaw
    # component of this and is kept only so callers that just want a direction
    # need not know the joint names.
    #
    # Yaw alone is not enough to look at someone: pitch is spread across
    # base/elbow/wrist, so a head left pointing at the floor sweeps the floor in
    # a circle no matter how right the yaw is (device-observed 2026-08-19 —
    # bearing stepped -45 to -13 correctly and still saw nothing).
    pose: Dict[str, float] = field(default_factory=dict)


def _path() -> str:
    return getattr(config, "USER_BEARING_PATH", "/var/lib/hal/user_bearing.json")


def _load_raw() -> Optional[dict]:
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    version = d.get("version")
    if version in (1, 2):
        # Every stored angle is in DEGREES ON A SPECIFIC CALIBRATION, and neither
        # of these schemas recorded which. homing_offset and range_min/max are
        # per physical unit and change on every recalibration — 6f0c4ec4 zeroed
        # all five offsets — so the same number names a different posture
        # afterwards. A v1/v2 file cannot be checked, and an unverifiable pose
        # restored confidently is exactly the failure this version exists to
        # stop: it aims the camera somewhere wrong at up to confidence 1.0 and
        # self-heals only after three clustered prediction misses.
        #
        # v1 used to be migrated by keeping its yaw. That is no longer safe
        # either: the recalibration moved base_yaw's scale too, so the direction
        # is as suspect as the posture. Re-learning costs about eight sightings.
        logger.info(
            "[user-bearing] dropping a v%s estimate — it predates calibration "
            "tracking, so its angles cannot be trusted on this arm", version,
        )
        return None
    if version != SCHEMA_VERSION:
        return None
    stored = d.get("calibration")
    live = _calibration_fingerprint()
    if live is None:
        # Cannot read the calibration at all. Be permissive rather than wiping
        # every estimate on the fleet over a missing file or a permissions
        # change — an unreadable calibration usually means the arm is not
        # running, not that the numbers moved.
        logger.debug("[user-bearing] calibration unreadable — accepting stored estimate")
        return d
    if stored != live:
        logger.info(
            "[user-bearing] calibration changed (%s -> %s) — dropping the stored "
            "pose; every angle in it describes the old arm",
            stored, live,
        )
        return None
    return d


def _write_raw(d: dict) -> bool:
    """Atomic write. A torn estimate file is worse than a missing one: it would
    be read as a confident bearing pointing somewhere arbitrary."""
    path = _path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except Exception as e:
        logger.debug("[user-bearing] write failed: %s", e)
        return False


def _confidence(samples: int) -> float:
    if samples <= 0:
        return 0.0
    return round(min(1.0, samples / float(CONFIDENCE_FULL_SAMPLES)), 4)


def _blend_pose(
    prev_pose: Dict[str, float], new_pose: Dict[str, float], alpha: float
) -> Dict[str, float]:
    """EMA each joint independently, at the same rate the yaw is smoothed.

    A joint present in only one side is taken as-is rather than dropped: the
    servo set can differ between reads (a joint that failed to report once must
    not erase what we already knew about it).
    """
    out: Dict[str, float] = dict(prev_pose)
    for joint, value in new_pose.items():
        if joint in prev_pose:
            out[joint] = round((1.0 - alpha) * float(prev_pose[joint]) + alpha * float(value), 3)
        else:
            out[joint] = round(float(value), 3)
    return out


def record_sighting(
    yaw_deg: float,
    pose: Optional[Dict[str, float]] = None,
    now: Optional[float] = None,
) -> bool:
    """Fold one CENTRED sighting into the estimate.

    Callers must only pass a yaw whose frame had the subject centred — this
    function cannot tell, and an off-centre yaw would silently bias the estimate
    by the uncorrected offset.
    """
    t = time.time() if now is None else now
    prev = _load_raw()

    new_pose: Dict[str, float] = {
        str(k): float(v) for k, v in (pose or {}).items() if isinstance(v, (int, float))
    }
    prev_pose: Dict[str, float] = (
        {str(k): float(v) for k, v in (prev.get("pose") or {}).items()} if prev else {}
    )

    streak = 0
    if prev:
        last = float(prev.get("updated", 0.0))
        if t - last < MIN_SAMPLE_INTERVAL_S:
            return False
        prev_bearing = float(prev.get("bearing_deg", yaw_deg))
        samples = int(prev.get("samples", 0)) + 1
        settled = _confidence(int(prev.get("samples", 0))) >= OUTLIER_MIN_CONFIDENCE

        if settled and abs(yaw_deg - prev_bearing) > OUTLIER_DEG:
            streak = int(prev.get("outlier_streak", 0)) + 1
            if streak >= OUTLIER_STREAK:
                # Not a passer-by — the user really is somewhere else now. The
                # old posture describes the old place, so replace it outright
                # rather than averaging toward the new one.
                bearing, samples, streak = yaw_deg, 1, 0
                blended = new_pose
            else:
                # Damp hard: one person crossing the room must not flip the estimate.
                bearing = (1.0 - OUTLIER_ALPHA) * prev_bearing + OUTLIER_ALPHA * yaw_deg
                blended = _blend_pose(prev_pose, new_pose, OUTLIER_ALPHA)
        else:
            bearing = (1.0 - EMA_ALPHA) * prev_bearing + EMA_ALPHA * yaw_deg
            blended = _blend_pose(prev_pose, new_pose, EMA_ALPHA)
    else:
        bearing = yaw_deg
        samples = 1
        blended = new_pose

    # One source of truth, and the yaw ALWAYS wins: a caller may record a
    # bearing with a partial pose (or none), and reading the yaw back out of the
    # blend would then silently keep the old direction and discard the sighting.
    blended[BASE_YAW_JOINT] = round(bearing, 3)

    ok = _write_raw({
        "version": SCHEMA_VERSION,
        # Stamped on every write, so a recalibration invalidates the estimate on
        # the next read rather than the next time somebody notices.
        "calibration": _calibration_fingerprint(),
        "bearing_deg": round(bearing, 3),
        "pose": blended,
        "confidence": _confidence(samples),
        "samples": samples,
        "outlier_streak": streak,
        "updated": t,
    })
    if ok:
        logger.info(
            "[user-bearing] sighting yaw=%+.1f -> estimate %+.1f (n=%d, joints=%d)",
            yaw_deg, bearing, samples, len(blended),
        )
    return ok


def read_estimate(now: Optional[float] = None) -> Optional[BearingEstimate]:
    """Current estimate, or None if never set.

    `confidence` reflects how many sightings built the estimate and does NOT
    fall with age; `age_s` carries the recency for callers that want it.
    """
    d = _load_raw()
    if not d:
        return None
    t = time.time() if now is None else now
    updated = float(d.get("updated", 0.0))
    age = max(0.0, t - updated)
    samples = int(d.get("samples", 0))
    return BearingEstimate(
        bearing_deg=float(d.get("bearing_deg", 0.0)),
        confidence=_confidence(samples),
        samples=samples,
        updated=updated,
        age_s=age,
        pose={str(k): float(v) for k, v in (d.get("pose") or {}).items()},
    )


def record_prediction(hit: bool, now: Optional[float] = None) -> bool:
    """Score one use of the bearing. Returns True if the estimate was dropped.

    `hit` = the aim turned to the remembered bearing and found a subject there.
    A miss is not conclusive on its own — the user may simply be out of the room —
    which is why it takes PREDICTION_MISS_LIMIT in a row before acting.
    """
    d = _load_raw()
    if not d:
        return False
    if hit:
        if d.get("misses"):
            d["misses"] = 0
            d["last_miss"] = 0.0
            _write_raw(d)
        return False

    t = time.time() if now is None else now
    last_miss = float(d.get("last_miss", 0.0))
    if last_miss > 0.0 and (t - last_miss) > MISS_STREAK_WINDOW_S:
        # Too long since the previous failure to be the same cause — start over.
        misses = 1
    else:
        misses = int(d.get("misses", 0)) + 1
    d["last_miss"] = t

    if misses >= PREDICTION_MISS_LIMIT:
        logger.info(
            "[user-bearing] %d failed predictions in a row — dropping estimate "
            "(lamp moved, or the user is no longer where they were)", misses,
        )
        clear()
        return True

    d["misses"] = misses
    _write_raw(d)
    logger.debug("[user-bearing] failed prediction %d/%d", misses, PREDICTION_MISS_LIMIT)
    return False


def clear() -> bool:
    """Forget the estimate — for "I moved you", and for the relocation handling
    in Task E. A moved lamp invalidates every stored bearing at once."""
    try:
        os.unlink(_path())
        logger.info("[user-bearing] estimate cleared")
        return True
    except FileNotFoundError:
        return True
    except Exception as e:
        logger.debug("[user-bearing] clear failed: %s", e)
        return False
