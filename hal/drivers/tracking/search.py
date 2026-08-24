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

Coverage: yaw spans -135..+135 and the camera sees ~100 deg (measured
107-123 on device; LOOK_AIM_FOV_DEG is set to 100, deliberately below the
measurement — see hal/config.py). STEP_DEG stays well under that, so stops
overlap and the sweep covers nearly the whole circle, leaving only a small
wedge directly behind the lamp.

The "~60-78 deg" this once claimed came from constants.CAMERA_FOV_DEG and the
hardware BOM, which disagree with each other and both with the device. The aim
stopped trusting a fixed FOV entirely (it measures the local scale per step);
this file only needs the number to be a lower bound on stop spacing, which 100
comfortably is.

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


def _current_yaw(svc: Any) -> float:
    try:
        return float(svc.get_positions().get("base_yaw.pos", 0.0))
    except Exception:
        return 0.0


def _seed_from_bearing(svc: Any) -> float:
    """Where to look first, and from what POSTURE.

    Restores the remembered pose before returning its yaw, because a sweep is
    the one place where yaw alone is provably not enough: it steps the head
    across up to MAX_STOPS bearings, and if the pitch is left aimed at the desk
    it sweeps the desk MAX_STOPS times and reports nobody there. That is
    `user_bearing`'s own warning — "a head left pointing at the floor sweeps the
    floor in a circle no matter how right the yaw is" — applied to the consumer
    that sweeps by definition.

    The other two consumers already restore the whole posture. This one read
    `bearing_deg` and nothing else, so the search was the only place the lesson
    of #226 had not landed.

    Confidence is honoured too, at the aim's permissive floor rather than
    gaze's: a seed is an ordering hint, and being wrong costs one extra stop out
    of eight, not a wasted turn. Below it, start from where the head already
    points and sweep outward from there.
    """
    try:
        from hal.drivers.tracking import aim, user_bearing

        est = user_bearing.read_estimate()
        if est is None:
            return _current_yaw(svc)
        if est.confidence < aim.MIN_BEARING_CONFIDENCE:
            logger.info(
                "[search] ignoring bearing %+.1f — confidence %.2f < %.2f",
                est.bearing_deg, est.confidence, aim.MIN_BEARING_CONFIDENCE,
            )
            return _current_yaw(svc)

        # Posture first, in ONE absolute move — the same mechanism the aim and
        # the gaze repoint use, so all three restore identically. An absolute
        # target has no sign to get wrong, which is why pitch is safe here and
        # not in a relative nudge.
        try:
            current = svc.get_positions()
            target, _step = aim._bearing_step_target(svc, est, current)
            if target:
                duration = aim.min_move_duration(
                    state.safety_policy, target, current, MOVE_DURATION_S
                )
                svc.move_and_hold(target, duration=duration)
                logger.info(
                    "[search] restored remembered posture (%d joints) before sweeping",
                    len(target),
                )
        except Exception as e:
            # A failed restore must not cost the search: sweeping from the
            # wrong pitch still beats not sweeping at all.
            logger.warning("[search] posture restore skipped: %s", e)

        return est.bearing_deg
    except Exception:
        return _current_yaw(svc)


def _seed_yaw(svc: Any) -> float:
    """Backwards-compatible alias — see _seed_from_bearing."""
    return _seed_from_bearing(svc)


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
        box, kind, _conf = _detect_subject(detector, frame)
        if box is not None:
            logger.info("[search] found %s at yaw %+.0f after %d stop(s)", kind, yaw, visited)
            return SearchResult(True, f"found {kind}", visited, yaw)

    return SearchResult(False, "nobody found", visited)
