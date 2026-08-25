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
# How long to wait for the arm to actually ARRIVE before settling and shooting.
#
# move_and_hold returns when it has finished sending frames, not when the servos
# have got there. Device-measured: a 90 deg base_yaw turn returns the call in
# 0.77s and is still moving at 5.88s, because base_yaw manages about 14 deg/s
# under the whole lamp's inertia while min_move_duration paces the interpolation
# for the declared 120 deg/s ceiling. Without this wait the head began its looks
# and the shutter fired while the base was still swinging — blurred frames,
# aimed somewhere other than the stop they are recorded against.
#
# Gains are not the cause and were checked: base_yaw runs at 14 deg/s at P=16
# and at P=32/I=10 alike, and every servo has Goal_Speed=0 (uncapped).
ARRIVE_TIMEOUT_S: float = 7.0
ARRIVE_STILL_DEG: float = 0.8
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

    After the seed it goes RIGHT, then left. That order is what lets the sweep
    flow: the seed stop finishes looking at seed+45, and the right stop opens on
    exactly the same direction (seed+90 with the head at -45), so the handover is
    invisible. Going left first would throw the head back across everything it
    had just covered.

    The one unavoidable jump is the last: the right stop ends at seed+135 and the
    left stop starts at seed-135. There is no ordering that avoids it, because
    the two ends of the sweep are simply far apart.

    Clamped rather than dropped at the mechanical limits: with only three stops
    a discarded one leaves a real hole, whereas a clamped one still looks
    somewhere useful. De-duplicated, so a seed near a limit yields fewer stops
    rather than the same stop twice.
    """
    seed = max(C.YAW_MIN, min(C.YAW_MAX, seed))
    span = (MAX_STOPS - 1) // 2
    stops: List[float] = [seed]
    # Right first (+1, +2, ...), then left (-1, -2, ...), so the head can carry
    # on rightward out of the seed before the one long trip back across.
    for i in list(range(1, span + 1)) + list(range(-1, -span - 1, -1)):
        y = max(C.YAW_MIN, min(C.YAW_MAX, seed + i * STEP_DEG))
        if y not in stops:
            stops.append(y)
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


def _wait_until_still(svc: Any, target: dict) -> None:
    """Block until the commanded joints stop moving, or the timeout bites.

    Waits for the joints to STOP rather than to reach their target: a stop the
    arm cannot quite reach is still a fine place to take a picture from, whereas
    waiting for an exact arrival that never comes would stall the whole sweep.
    """
    deadline = time.monotonic() + ARRIVE_TIMEOUT_S
    last: Optional[dict] = None
    while time.monotonic() < deadline:
        try:
            now_pose = svc.get_positions()
        except Exception:
            return
        if last is not None and all(
            abs(float(now_pose.get(j, 0.0)) - float(last.get(j, 0.0))) < ARRIVE_STILL_DEG
            for j in target
        ):
            return
        last = now_pose
        time.sleep(0.1)
    logger.info("[search] still moving after %.1fs — shooting anyway", ARRIVE_TIMEOUT_S)


def _look_at(svc: Any, roll: float, yaw: Optional[float] = None) -> bool:
    """Point the camera at one look. False if the move could not be made.

    When `yaw` is given the base and the head move TOGETHER, in one command, and
    that is the whole reason the sweep flows. A stop ends looking at yaw+45; the
    next stop opens at yaw+90 with the head at -45, which is the same direction.
    Move the base first and the head second and the view flies out to yaw+135 and
    comes back — device-traced as +48 -> +138 -> +48, a 90 deg out-and-back
    wobble at every handover. Travelling together, the two rotations cancel and
    the camera simply holds its line while the lamp rearranges itself under it.
    """
    try:
        from hal.drivers.tracking import aim

        current = svc.get_positions()
        target = {"wrist_roll.pos": float(roll)}
        if yaw is not None:
            target["base_yaw.pos"] = float(yaw)
        if all(abs(v - float(current.get(j, 0.0))) <= 0.5 for j, v in target.items()):
            return True
        duration = aim.min_move_duration(
            state.safety_policy, target, current, MOVE_DURATION_S
        )
        svc.move_and_hold(target, duration=duration)
        _wait_until_still(svc, target)
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

    # Own the body for the whole sweep. Without this the idle recording keeps
    # playing straight through it, absolutely and on every joint, and every stop
    # the search commands is overwritten by the next idle frame ~33ms later.
    #
    # Device-traced 2026-08-25 during one sweep: idle wrote base_yaw 280 times
    # to the search's 31. The visible result was a base that crawled — 90 deg
    # took 5.9s with HAL running, against 0.35s for the same move with the arm
    # to itself. Not a slow servo, a contested one.
    from hal.drivers.tracking import aim

    with aim.servo_ownership():
        return _sweep(svc, cap, detector, target)


def _sweep(svc: Any, cap: Any, detector: Any, target: str) -> SearchResult:
    """The sweep itself, with the body already owned."""
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

        # Look around from here before turning the body again. Each angle is a
        # STOP, not a pan-through — a head still moving gives a blurred frame
        # and a detector that misses what is plainly in view.
        #
        # Always left to right, and the stop ORDER is what makes that smooth.
        # A stop ends at roll +45, looking at yaw+45; the next stop to the right
        # is yaw+90, whose first look at roll -45 is also yaw+45 — the same
        # direction. The base turns +90 while the head turns -90 and the camera
        # never leaves the spot.
        #
        # Alternating the roll direction instead was tried and is worse: it
        # destroys precisely that handover, because the next stop then opens
        # where the last one already was and the head has nowhere to carry on to.
        for n, roll in enumerate(ROLL_STOPS):
            if _abort_evt.is_set():
                return _abandon(svc, seed_pose, visited)
            # The first look of a stop carries the base turn with it, so the
            # handover from the previous stop is one continuous movement.
            if not _look_at(svc, roll, yaw=yaw if n == 0 else None):
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

    # Nothing found, so nothing to look at — go back to where the sweep began
    # rather than freezing wherever the last look left the head.
    _restore(svc, seed_pose)
    logger.info("[search] nobody found after %d stop(s) — back to the starting pose",
                visited)
    return SearchResult(False, "nobody found", visited)
