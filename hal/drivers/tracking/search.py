"""Deliberate search sweep — asked for, never inline.

The look-aim (`aim.py`) never sweeps. It runs inside a live conversational turn
under a hard deadline, and a 2-3.5 s search there would be exactly the dead air
the design exists to avoid — so when it cannot find anyone it captures from
wherever it reached.

This is the opposite: slow, thorough, and entered only where the time is
affordable —

  * the user asks outright — "where are you?", "find my cup"
  * they accept an offer after a failed look — "I can't see it. Want me to
    look around?"

Coverage: yaw spans -135..+135 and the camera sees ~60-78 deg, so stepping in
sub-FOV increments covers nearly the whole circle, leaving only a small wedge
directly behind the lamp.

Search order is seeded from the remembered bearing and expands outward rather
than sweeping left-to-right, because the most likely place is worth looking at
first — that is what usually turns a multi-second sweep into one stop.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, List, Optional

import hal.app_state as state
from hal.drivers.tracking import constants as C
from hal.drivers.tracking.aim import _detect_subject, _grab_frame

logger = logging.getLogger(__name__)

# Stops must overlap: stepping by the full FOV would leave seams where a person
# straddling two tiles is missed by both.
STEP_DEG: float = 45.0
# Servos are commanded, then given time to stop ringing before a frame is read.
# SERVO_SMOOTH_TIME (0.32) is the easing constant this mirrors.
SETTLE_S: float = 0.35
MOVE_DURATION_S: float = 0.3
MAX_STOPS: int = 8

_abort_evt = threading.Event()


def request_abort() -> None:
    """Stop an in-flight search. Wired to the physical button, whose single
    click means "stop moving and pay attention to me"."""
    _abort_evt.set()


@dataclass
class SearchResult:
    found: bool
    reason: str
    stops_visited: int = 0
    found_at_yaw: Optional[float] = None


def _stop_list(seed: float) -> List[float]:
    """Yaw stops ordered by likelihood: the seed first, then alternating
    outward. Clamped to the mechanical range and de-duplicated."""
    stops: List[float] = []
    seed = max(C.YAW_MIN, min(C.YAW_MAX, seed))
    stops.append(seed)
    step = 1
    while len(stops) < MAX_STOPS:
        added = False
        for sign in (1, -1):
            y = seed + sign * step * STEP_DEG
            if C.YAW_MIN <= y <= C.YAW_MAX and y not in stops:
                stops.append(y)
                added = True
                if len(stops) >= MAX_STOPS:
                    break
        if not added:
            break  # both directions have run off the mechanical limits
        step += 1
    return stops


def _seed_yaw(svc: Any) -> float:
    """Where to look first: the remembered bearing when we have one, else
    wherever the head already points."""
    try:
        from hal.drivers.tracking import user_bearing

        est = user_bearing.read_estimate()
        if est is not None:
            return est.bearing_deg
    except Exception:
        pass
    try:
        return float(svc.get_positions().get("base_yaw.pos", 0.0))
    except Exception:
        return 0.0


def search_for_subject(target: str = "person", detector: Any = None) -> SearchResult:
    """Sweep for a subject, stopping at the first one seen.

    Returns rather than raising: a failed search still has to give the caller
    something to say.
    """
    _abort_evt.clear()

    cap = getattr(state, "camera_capture", None)
    svc = getattr(state, "animation_service", None)
    if cap is None or svc is None:
        return SearchResult(False, "no camera or animation service")
    if getattr(state, "_camera_disabled", False):
        # Privacy: a search is a lot of conspicuous movement to perform while
        # the user has asked the device not to look.
        return SearchResult(False, "camera disabled")

    if detector is None:
        from hal.drivers.tracking.aim import get_detector

        detector = get_detector()
        if detector is None:
            return SearchResult(False, "no detector")

    stops = _stop_list(_seed_yaw(svc))
    logger.info("[search] sweeping %d stops for '%s': %s",
                len(stops), target, [round(s) for s in stops])

    visited = 0
    for yaw in stops:
        if _abort_evt.is_set():
            return SearchResult(False, "aborted", visited)

        try:
            current = svc.get_positions()
            delta = yaw - float(current.get("base_yaw.pos", 0.0))
            if abs(delta) > 0.5:
                svc.nudge(delta, 0.0, MOVE_DURATION_S, current, state.safety_policy)
        except Exception as e:
            logger.warning("[search] move to %+.0f failed: %s", yaw, e)
            return SearchResult(False, f"move failed: {e}", visited)

        # Settle before reading: a head still ringing gives a blurred frame and
        # a detector that misses what is actually in view.
        time.sleep(SETTLE_S)
        visited += 1

        frame = _grab_frame(cap)
        if frame is None:
            continue
        box, kind = _detect_subject(detector, frame)
        if box is not None:
            logger.info("[search] found %s at yaw %+.0f after %d stop(s)", kind, yaw, visited)
            return SearchResult(True, f"found {kind}", visited, yaw)

    return SearchResult(False, "nobody found", visited)
