"""A narrated tour of the joint limits — "show me what you can do".

Not a search. Nothing is detected and nothing is reported; the movement and the
words that go with it ARE the deliverable.

It exists because that request had nowhere to go. `skills/servo-control` bundled
"show me your maximum capability" onto the same trigger as "scan the whole
room", so a request to SHOW the range and a request to COVER the room resolved
to one endpoint — and when the skill did not match at all, the model reached for
the nearest thing that looked like a sweep: the `scan` emotion, a hand-recorded
animation that covers 54.4 deg of a 270 deg travel while the camera sits idle,
narrated as "I'll sweep my whole range… doing a full turn now!".

Computed from the limits, never recorded. A recording cannot be right by
construction: `hal/recordings/scanning.csv` is 360 frames of somebody's hand
movement and nothing in the file says how far it actually reaches. Reading
YAW_MIN/YAW_MAX means the demo stays honest if the limits change, and ports to
another robot's constants for free.

Speech rides the filler-pool path (`aim._say` -> `/api/sensing/filler`), the
same one the sweep's midpoint phrase uses: os-server owns the words, the
language and the WAV cache, HAL decides only WHEN. That is what lets "all the
way left" land WITH the movement. A `[HW:...]` marker cannot do this — markers
fire before TTS, so a marker-narrated demo describes a performance that has
already finished.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, List, Optional, Tuple

import hal.app_state as state
from hal.drivers.tracking import constants as C
from hal.drivers.tracking.search import _wait_until_still

logger = logging.getLogger(__name__)

# Requested duration for one leg. The safety policy stretches it when the move
# would exceed SAFETY.md's max_speed, so this is a floor rather than a promise.
LEG_DURATION_S: float = 1.6
# Held at each end so the reach reads as a limit rather than a turning point.
DWELL_S: float = 0.4

# How fast the base may turn WHILE DEMONSTRATING, in STS3215 Goal_Speed units.
#
# Same register, same reason, same value as the sweep: base_yaw manages about
# 14 deg/s untouched, which makes a 135 deg leg take ~9 s — long enough that the
# phrase describing it finishes while the lamp is still swinging. 1200 gives
# roughly 80 deg/s. Written here rather than at startup because nothing else
# wants a faster base: idle, emotions and every recorded animation move in small
# 30fps steps and are paced deliberately.
DEMO_YAW_SPEED: int = 1200

# How far the head tilts from the seed pose, up and down.
#
# NOT the mechanical limit, deliberately. WRIST_PITCH_MIN/MAX say +/-90 and the
# arm does not have that: device-measured on lamp-ac82, wrist_pitch reached
# -89.55 going up (stopped by the soft limit, not the joint) and only -16.61
# going down with no stall at all. The declared range is not a measurement, and
# a demo that drives to it would stall the joint against a number nobody has
# checked — on a lamp whose whole point here is to look deliberate.
#
# So the yaw legs go to the real limits (that IS the headline: 270 deg, not 54)
# while the pitch legs reuse the offset the sweep's look ring has been walking
# every time it runs. Proven travel beats a declared one.
PITCH_LOOK_DEG: float = 25.0
# Margin held off the soft stop, mirroring the sweep's WRIST_PITCH_MARGIN:
# commanding the exact limit stalls the servo against it.
PITCH_MARGIN: float = 2.0

# How long to wait for TTS to clear before the next line. A demo that talks over
# its own previous phrase sounds broken, and the opening reply of the turn that
# triggered it is often still playing when the first leg starts.
TTS_WAIT_TIMEOUT_S: float = 3.0

_abort_evt = threading.Event()
_running = threading.Event()


def request_abort() -> None:
    """Stop an in-flight demo.

    Wired to the physical button alongside the aim and the search: one click
    means "stop moving and pay attention to me", and a lamp that keeps narrating
    through it is worse than one that merely keeps moving.
    """
    _abort_evt.set()


def is_running() -> bool:
    return _running.is_set()


def waypoints(seed_pose: dict) -> List[Tuple[dict, str]]:
    """(absolute pose, phrase pool) for each leg, in performance order.

    Absolute rather than relative, so re-entering the demo cannot accumulate an
    offset into a leg — the same discipline the sweep's stop list follows.

    Yaw first and left before right, because that is the order the phrases are
    written in: a demo that says "all the way left" while turning right is the
    defect this replaces, wearing different clothes.

    Pitch is clamped against the SEED pose, not assumed symmetric. A lamp
    resting head-low has less headroom above it than below, and a commanded
    overrun just stalls.
    """
    lo = C.WRIST_PITCH_MIN + PITCH_MARGIN
    hi = C.WRIST_PITCH_MAX - PITCH_MARGIN
    wps: List[Tuple[dict, str]] = [
        ({"base_yaw.pos": C.YAW_MIN}, "demo_left"),
        ({"base_yaw.pos": C.YAW_MAX}, "demo_right"),
    ]
    base_wp = seed_pose.get("wrist_pitch.pos")
    if base_wp is not None:
        # Negative is UP, positive is DOWN — gaze._maybe_pitch's convention,
        # shared with the sweep's look ring.
        up = max(lo, min(hi, float(base_wp) - PITCH_LOOK_DEG))
        down = max(lo, min(hi, float(base_wp) + PITCH_LOOK_DEG))
        wps.append(({"wrist_pitch.pos": up}, "demo_up"))
        wps.append(({"wrist_pitch.pos": down}, "demo_down"))
    return wps


def _wait_for_tts() -> None:
    """Let the previous phrase finish.

    Best-effort and bounded: a stuck speaking flag must not freeze the body
    part-way through a performance.
    """
    tts = getattr(state, "tts_service", None)
    if tts is None:
        return
    deadline = time.monotonic() + TTS_WAIT_TIMEOUT_S
    while time.monotonic() < deadline:
        if not getattr(tts, "speaking", False):
            return
        time.sleep(0.1)


def _go(svc: Any, pose: dict) -> None:
    """Command one leg and wait for the body to actually get there.

    The wait is the point. `move_and_hold` returns when it has finished SENDING
    frames, not when the servos have arrived — device-measured, a 90 deg base
    turn returns the call in 0.77 s and is still moving at 5.88 s. Speaking on
    the call's return would put every phrase seconds ahead of the movement it
    describes, which is the whole failure this demo exists to correct.
    """
    from hal.drivers.tracking import aim

    current = svc.get_positions()
    duration = aim.min_move_duration(state.safety_policy, pose, current,
                                     LEG_DURATION_S)
    svc.move_and_hold(pose, duration=duration)
    _wait_until_still(svc, pose)


def run(svc: Any) -> dict:
    """Perform the demo. Blocking — `start()` is what callers use."""
    from hal.drivers.tracking import aim

    _abort_evt.clear()
    _running.set()
    try:
        seed_pose = {j: float(v) for j, v in svc.get_positions().items()
                     if j.endswith(".pos")}
    except Exception as e:
        _running.clear()
        logger.warning("[demo] could not read the starting pose: %s", e)
        return {"completed": False, "reason": f"no pose: {e}", "waypoints": 0}

    wps = waypoints(seed_pose)
    logger.info("[demo] %d leg(s), yaw %.0f..%.0f, pitch %+.0f from seed",
                len(wps), C.YAW_MIN, C.YAW_MAX, PITCH_LOOK_DEG)
    done = 0
    try:
        with aim.servo_ownership():
            capped = svc.set_joint_speed("base_yaw", DEMO_YAW_SPEED)
            try:
                _wait_for_tts()
                aim._say("demo_intro")
                for pose, pool in wps:
                    if _abort_evt.is_set():
                        _go(svc, seed_pose)
                        logger.info("[demo] aborted after %d leg(s)", done)
                        return {"completed": False, "reason": "aborted",
                                "waypoints": done}
                    _wait_for_tts()
                    aim._say(pool)
                    _go(svc, pose)
                    time.sleep(DWELL_S)
                    done += 1
                _go(svc, seed_pose)
                _wait_for_tts()
                aim._say("demo_done")
            finally:
                if capped:
                    # Back to the resting value the driver writes at startup.
                    # A cap left behind would follow the demo out and throttle
                    # idle and every emotion, none of which asked to be paced.
                    svc.set_joint_speed(
                        "base_yaw",
                        getattr(svc, "UNWRITTEN_SPEED_EQUIVALENT", 0),
                    )
        return {"completed": True, "reason": "done", "waypoints": done}
    except Exception as e:
        logger.warning("[demo] failed after %d leg(s): %s", done, e)
        return {"completed": False, "reason": str(e), "waypoints": done}
    finally:
        _running.clear()


def start(svc: Any) -> dict:
    """Kick the demo off on its own thread and return at once.

    Non-blocking because the caller is the agent's `[HW:/servo/demo:{}]` marker,
    and `fireHWCall` gives a hardware POST five seconds (handler_hw.go) while the
    demo runs ~20. A blocking route would time out mid-performance — and on that
    path the timeout returns before reaching any `flow.Log`, so the whole
    performance would be invisible to the Monitor as well.
    """
    if getattr(state, "_sleeping", False):
        logger.info("[demo] ignored -- device is sleeping")
        return {"started": False, "waypoints": 0, "reason": "sleeping"}
    if _running.is_set():
        logger.info("[demo] ignored -- already running")
        return {"started": False, "waypoints": 0, "reason": "already running"}
    wps = 0
    try:
        wps = len(waypoints(svc.get_positions()))
    except Exception as e:
        logger.debug("[demo] could not count legs before starting: %s", e)
    threading.Thread(target=run, args=(svc,), name="range-demo",
                     daemon=True).start()
    return {"started": True, "waypoints": wps, "reason": "started"}
