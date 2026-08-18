"""Remember roughly where the user is, as a single servo yaw.

The lamp only ever needs ONE angle to turn to, so this deliberately stores one
decaying estimate rather than a per-user, per-hour histogram. When the user is
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

import json
import logging
import math
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Optional

import hal.config as config

logger = logging.getLogger(__name__)

SCHEMA_VERSION: int = 1

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
# Confidence reaches ~1.0 after this many sightings...
CONFIDENCE_FULL_SAMPLES: int = 8
# ...and decays with this time constant once they stop, so a stale estimate
# reports itself as stale instead of looking authoritative.
CONFIDENCE_HALFLIFE_S: float = 6 * 3600.0


@dataclass
class BearingEstimate:
    bearing_deg: float
    confidence: float
    samples: int
    updated: float          # epoch seconds
    age_s: float


def _path() -> str:
    return getattr(config, "USER_BEARING_PATH", "/var/lib/hal/user_bearing.json")


def _load_raw() -> Optional[dict]:
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    if not isinstance(d, dict) or d.get("version") != SCHEMA_VERSION:
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


def _confidence(samples: int, age_s: float) -> float:
    grown = min(1.0, samples / float(CONFIDENCE_FULL_SAMPLES)) if samples > 0 else 0.0
    decay = math.exp(-max(0.0, age_s) / CONFIDENCE_HALFLIFE_S)
    return round(grown * decay, 4)


def record_sighting(yaw_deg: float, now: Optional[float] = None) -> bool:
    """Fold one CENTRED sighting into the estimate.

    Callers must only pass a yaw whose frame had the subject centred — this
    function cannot tell, and an off-centre yaw would silently bias the estimate
    by the uncorrected offset.
    """
    t = time.time() if now is None else now
    prev = _load_raw()

    streak = 0
    if prev:
        last = float(prev.get("updated", 0.0))
        if t - last < MIN_SAMPLE_INTERVAL_S:
            return False
        prev_bearing = float(prev.get("bearing_deg", yaw_deg))
        samples = int(prev.get("samples", 0)) + 1
        settled = _confidence(int(prev.get("samples", 0)), t - last) >= OUTLIER_MIN_CONFIDENCE

        if settled and abs(yaw_deg - prev_bearing) > OUTLIER_DEG:
            streak = int(prev.get("outlier_streak", 0)) + 1
            if streak >= OUTLIER_STREAK:
                # Not a passer-by — the user really is somewhere else now.
                bearing, samples, streak = yaw_deg, 1, 0
            else:
                # Damp hard: one person crossing the room must not flip the estimate.
                bearing = (1.0 - OUTLIER_ALPHA) * prev_bearing + OUTLIER_ALPHA * yaw_deg
        else:
            bearing = (1.0 - EMA_ALPHA) * prev_bearing + EMA_ALPHA * yaw_deg
    else:
        bearing = yaw_deg
        samples = 1

    ok = _write_raw({
        "version": SCHEMA_VERSION,
        "bearing_deg": round(bearing, 3),
        "confidence": _confidence(samples, 0.0),
        "samples": samples,
        "outlier_streak": streak,
        "updated": t,
    })
    if ok:
        logger.info(
            "[user-bearing] sighting yaw=%+.1f -> estimate %+.1f (n=%d)",
            yaw_deg, bearing, samples,
        )
    return ok


def read_estimate(now: Optional[float] = None) -> Optional[BearingEstimate]:
    """Current estimate with confidence decayed to now, or None if never set."""
    d = _load_raw()
    if not d:
        return None
    t = time.time() if now is None else now
    updated = float(d.get("updated", 0.0))
    age = max(0.0, t - updated)
    samples = int(d.get("samples", 0))
    return BearingEstimate(
        bearing_deg=float(d.get("bearing_deg", 0.0)),
        confidence=_confidence(samples, age),
        samples=samples,
        updated=updated,
        age_s=age,
    )


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
