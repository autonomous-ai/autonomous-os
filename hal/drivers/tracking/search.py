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

# How far the base turns between stops.
#
# 90, not 45, because the head now looks around at each stop and covers the gap.
# With ROLL_STOPS at +/-45 and a ~100 deg lens (LOOK_AIM_FOV_DEG), one yaw stop
# sees a continuous span of yaw+/-95:
#
#   roll -45  ->  yaw-95 .. yaw+5
#   roll   0  ->  yaw-50 .. yaw+50
#   roll +45  ->  yaw+5  .. yaw+95
#
# So stops at seed and seed+/-90 cover seed+/-185 — the whole circle, with
# overlap and no seams. Stepping by 45 as before would re-check ground the head
# has already covered, at three detections a time.
STEP_DEG: float = 90.0
# Servos are commanded, then given time to stop ringing before a frame is read.
# SERVO_SMOOTH_TIME (0.32) is the easing constant this mirrors.
SETTLE_S: float = 0.35
MOVE_DURATION_S: float = 0.3
MAX_STOPS: int = 3

# Where the head looks at each yaw stop, in order: left, straight on, right —
# then back to centre before the base turns again.
#
# wrist_roll rather than more base_yaw because the two are not equivalent to
# watch. Turning the whole lamp reads as a camera on a turntable; turning the
# head at a fixed body reads as something looking around. Device-measured the
# same day: roll pans the view while leaving the horizon level (it aims the
# camera, it does not rotate the image), and it reached every target from -59
# to +59 cleanly — so +/-45 is comfortably inside its travel and cannot tilt
# the camera toward the floor part-way through a sweep.
#
# Absolute, not relative to the seed: at roll 0 the camera looks along base_yaw,
# which is what makes a yaw stop mean what the stop list says it means.
ROLL_LOOK_DEG: float = 45.0
ROLL_STOPS = (-ROLL_LOOK_DEG, 0.0, ROLL_LOOK_DEG)
ROLL_CENTRE: float = 0.0

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
    """The seed first, then the remaining stops left to right.

    Two things are being bought at once, and they pull against each other.

    The seed goes first because the sweep stops on the FIRST subject it sees,
    and "first" should mean the person who was asked about. Device-observed
    2026-08-25 with pure left-to-right ordering: the sweep found a person at
    yaw -102 — a colleague at another desk — while the user sat at the seed,
    -12, which it never reached. Ordering by position alone answers "is anyone
    in this room" when the question was "where are YOU".

    Everything after that runs left to right, because the base swinging back and
    forth across centre (seed, +90, -90) reads as agitation once the head is
    also looking around at each stop. One reversal on the way out is enough.

    Clamped rather than dropped at the mechanical limits: with only three stops
    a discarded one leaves a real hole, whereas a clamped one still looks
    somewhere useful. De-duplicated, so a seed near a limit yields fewer stops
    rather than the same stop twice.
    """
    seed = max(C.YAW_MIN, min(C.YAW_MAX, seed))
    span = (MAX_STOPS - 1) // 2
    rest: List[float] = []
    for i in range(-span, span + 1):
        if i == 0:
            continue
        y = max(C.YAW_MIN, min(C.YAW_MAX, seed + i * STEP_DEG))
        if y != seed and y not in rest:
            rest.append(y)
    return [seed] + sorted(rest)


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
            seeded = _rest_on_idle_pose(svc)
            return _current_yaw(svc) if seeded is None else seeded
        if est.confidence < aim.MIN_BEARING_CONFIDENCE:
            logger.info(
                "[search] ignoring bearing %+.1f — confidence %.2f < %.2f",
                est.bearing_deg, est.confidence, aim.MIN_BEARING_CONFIDENCE,
            )
            seeded = _rest_on_idle_pose(svc)
            return _current_yaw(svc) if seeded is None else seeded

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
        seeded = _rest_on_idle_pose(svc)
        return _current_yaw(svc) if seeded is None else seeded


def _look_at_roll(svc: Any, roll: float) -> bool:
    """Turn the head to an absolute wrist_roll angle. False if it could not."""
    try:
        from hal.drivers.tracking import aim

        current = svc.get_positions()
        if abs(roll - float(current.get("wrist_roll.pos", 0.0))) <= 0.5:
            return True
        target = {"wrist_roll.pos": float(roll)}
        duration = aim.min_move_duration(
            state.safety_policy, target, current, MOVE_DURATION_S
        )
        svc.move_and_hold(target, duration=duration)
        return True
    except Exception as e:
        logger.warning("[search] look to roll %+.0f failed: %s", roll, e)
        return False


def _abandon(svc: Any, seed_pose: Optional[dict], visited: int) -> "SearchResult":
    """End an aborted sweep on the pose it started from.

    "Stop moving" is the request, and the pose the sweep happens to be frozen in
    is not a resting one — the head can be cocked 45 deg over, facing a wall.
    Stopping there answers the letter of the request and none of it: what the
    click asks for is the lamp to stop searching and attend to the person, which
    means ending somewhere it can see them from.

    Both abort checks route through here, so there is one definition of where an
    interrupted search leaves the arm.
    """
    _restore(svc, seed_pose)
    logger.info("[search] aborted after %d stop(s) — back to the starting pose", visited)
    return SearchResult(False, "aborted", visited)


def _restore(svc: Any, pose: Optional[dict]) -> None:
    """Put the arm back on a remembered pose. Never raises."""
    if not pose:
        return
    try:
        from hal.drivers.tracking import aim

        current = svc.get_positions()
        duration = aim.min_move_duration(
            state.safety_policy, pose, current, MOVE_DURATION_S
        )
        svc.move_and_hold(pose, duration=duration)
    except Exception as e:
        logger.warning("[search] could not return to the starting pose: %s", e)


def _straighten_head_onto(svc: Any, yaw: float, roll: float) -> None:
    """Keep looking where the subject was found, but with the head level.

    A search that ends the moment it sees someone ends with the head cocked
    wherever it happened to be looking — up to 45 deg over. Returning to the
    seed would fix the posture and lose the subject; turning the BASE by as much
    as the head is turned keeps the camera pointed at exactly the same place
    while the head comes back to centre.
    """
    aimed_at = yaw + roll
    settled = max(C.YAW_MIN, min(C.YAW_MAX, aimed_at))
    # Whatever the base cannot absorb stays in the head, so the camera still
    # points at the subject even when the turn runs into the mechanical limit.
    try:
        from hal.drivers.tracking import aim

        target = {"base_yaw.pos": settled, "wrist_roll.pos": aimed_at - settled}
        current = svc.get_positions()
        duration = aim.min_move_duration(
            state.safety_policy, target, current, MOVE_DURATION_S
        )
        svc.move_and_hold(target, duration=duration)
    except Exception as e:
        logger.warning("[search] could not straighten onto the subject: %s", e)


def _rest_on_idle_pose(svc: Any) -> Optional[float]:
    """Stand the arm on the idle recording's own pose, and return its yaw.

    The fallback for a device with no bearing yet — a fresh unit, or one whose
    bearing was reset. Without it the sweep started from wherever the arm
    happened to be, which on a loop that has just been walking the head around
    is not a pose anyone chose. A sweep from a camera aimed at the desk finds
    nothing however thorough it is.

    The idle baseline is the recording's first frame, which the animation
    service already holds — no file parsing, and it is by construction a pose
    the lamp is designed to rest in. Device-checked 2026-08-25: it looks out at
    head height with the room in view, so the "not aimed at the floor"
    guarantee comes from the pose itself and needs no separate pitch check.
    """
    baseline = getattr(svc, "_idle_baseline", None)
    if not isinstance(baseline, dict) or not baseline:
        return None
    target = {j: float(v) for j, v in baseline.items() if j.endswith(".pos")}
    if not target:
        return None
    try:
        from hal.drivers.tracking import aim

        current = svc.get_positions()
        duration = aim.min_move_duration(
            state.safety_policy, target, current, MOVE_DURATION_S
        )
        svc.move_and_hold(target, duration=duration)
        logger.info(
            "[search] no bearing yet — resting on the idle pose (%d joints) before sweeping",
            len(target),
        )
        return float(target.get("base_yaw.pos", _current_yaw(svc)))
    except Exception as e:
        logger.warning("[search] idle-pose restore skipped: %s", e)
        return None


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
    # Captured AFTER seeding, so it is the pose the sweep started from
    # rather than whatever the arm was doing before — that is where a
    # failed search should leave the lamp.
    try:
        seed_pose = {j: float(v) for j, v in svc.get_positions().items()
                     if j.endswith('.pos')}
    except Exception:
        seed_pose = None
    logger.info("[search] sweeping %d stops for '%s': %s",
                len(stops), target, [round(s) for s in stops])

    visited = 0
    for yaw in stops:
        if _abort_evt.is_set():
            return _abandon(svc, seed_pose, visited)

        try:
            current = svc.get_positions()
            delta = yaw - float(current.get("base_yaw.pos", 0.0))
            if abs(delta) > 0.5:
                svc.nudge(delta, 0.0, MOVE_DURATION_S, current, state.safety_policy)
        except Exception as e:
            logger.warning("[search] move to %+.0f failed: %s", yaw, e)
            return SearchResult(False, f"move failed: {e}", visited)

        # Look around from here before turning the body again: left, centre,
        # right. Each is a STOP, not a pan-through — a head still moving gives a
        # blurred frame and a detector that misses what is plainly in view.
        for roll in ROLL_STOPS:
            if _abort_evt.is_set():
                return _abandon(svc, seed_pose, visited)
            if not _look_at_roll(svc, roll):
                continue

            # Settle before reading: a head still ringing gives a blurred frame
            # and a detector that misses what is actually in view.
            time.sleep(SETTLE_S)
            visited += 1

            frame = _grab_frame(cap)
            if frame is None:
                continue
            box, kind, _conf = _detect_subject(detector, frame)
            if box is not None:
                logger.info(
                    "[search] found %s at yaw %+.0f roll %+.0f after %d stop(s)",
                    kind, yaw, roll, visited,
                )
                _straighten_head_onto(svc, yaw, roll)
                return SearchResult(True, f"found {kind}", visited, yaw)

        # Head back to centre before the base turns, so the next yaw stop looks
        # where the stop list says it does rather than 45 deg off it.
        _look_at_roll(svc, ROLL_CENTRE)

    # Nothing found, so nothing to look at — go back to where the sweep began
    # rather than freezing wherever the last look left the head.
    _restore(svc, seed_pose)
    logger.info("[search] nobody found after %d stop(s) — back to the starting pose",
                visited)
    return SearchResult(False, "nobody found", visited)
