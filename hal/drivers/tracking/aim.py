"""One-shot aim for visual questions ("what am I holding?", "look at this").

When the realtime model calls the `look` tool, the head may be pointing
anywhere — `look` takes no parameters and grabs whatever the camera currently
sees, so the model can answer confidently about a wall. This centres the
subject *before* the shutter, bounded by a deadline so a live turn never
stalls waiting for servos.

Scope (v1) — yaw only, deliberately:
  * The yaw sign is COPIED from the tracker's empirically verified convention
    (`tracker_service.py`: "dx>0 (object on right) -> base_yaw must INCREASE to
    chase right (verified empirically vs legacy gimbal path)"). It is not
    re-derived; a sign error here is silent and mirrors every move.
  * Pitch is excluded. `AnimationService.nudge()` drives `base_pitch`, while the
    tracker distributes pitch across base/elbow/wrist — so the pitch sign is
    NOT validated for this path, and an inverted pitch is the exact bug this
    codebase already hit once (see servo_follow.command_pid docstring).

Safety: every move goes through `AnimationService.nudge()`, which applies
`min_move_duration(safety_policy, ...)` — so SAFETY.md's `max_speed` ceiling
stretches the move rather than being bypassed to hit the deadline.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

import hal.config as config
from hal.safety.policy import min_move_duration
from hal.drivers.tracking import look_debug

logger = logging.getLogger(__name__)

# How close to frame centre counts as "aimed", as a fraction of frame width.
# Wide enough that the lamp does not hunt for a perfect centre it cannot hold.
CENTRE_DEADBAND_FRAC: float = 0.06
# Proportional gain on the measured offset. Below 1.0 so a slightly wrong
# deg_per_px (the FOV constant is unverified — see the plan's Task P) converges
# instead of oscillating.
AIM_GAIN: float = 0.85
# Optimistic move duration. The safety policy stretches it when the move would
# exceed SAFETY.md max_speed, so this is a floor, not a promise.
MOVE_DURATION_S: float = 0.25
# Hard cap on correction rounds; the deadline usually bites first.
MAX_ITERATIONS: int = 6
# Recording a bearing needs a TIGHTER centre than framing does. The aim stops at
# CENTRE_DEADBAND_FRAC because that frames the subject well enough, but at that
# offset the servo position is not the bearing — it is the bearing plus up to
# 0.06 x FOV of uncorrected error. Only a near-exact centre lets us store
# `bearing = base_yaw` and stay independent of the disputed FOV constant.
RECORD_DEADBAND_FRAC: float = 0.02
# Priority 2 — occlusion hysteresis. A subject that vanishes seconds after being
# seen at this pose is almost certainly OCCLUDED (a held-up object covering
# them), not absent. Turning away then would abandon the very thing we were
# asked to look at.
RECENT_SIGHTING_S: float = 4.0
RECENT_SIGHTING_YAW_TOL_DEG: float = 25.0
# Priority 3 — remembered-bearing fallback, taken in STEPS. nudge() blocks until
# the move completes, so a single large move travels blind and can pass straight
# over someone standing between here and there. Keep each step well under the
# camera FOV so detection runs at least once per FOV of travel.
# Retained as the cap on how far one bearing move may travel in a single
# command, not as a hop size — the move goes directly to the remembered pose.
BEARING_STEP_DEG: float = 20.0
MAX_BEARING_STEPS: int = 3
# Below this the estimate is too green or too stale to be worth turning for.
MIN_BEARING_CONFIDENCE: float = 0.2

# Last confirmed sighting: monotonic timestamp and the yaw it was seen at.
_last_seen_mono: float = 0.0
_last_seen_yaw: float = 0.0

# ONE detector for the process. Building an ObjectDetector is not cheap: with
# DL encryption on, its constructor fetches the public key over the network. A
# per-call detector made a single aim iteration take ~7s on device, which blew
# the realtime turn's budget — Gemini timed out, the turn fell back to the main
# agent, and the user got "I couldn't see it" for a frame that was captured
# perfectly. TrackerService builds its detector once for the same reason.
_detector_lock = threading.Lock()
# Held around inference. The detector is a single shared model and concurrent
# detect() calls on it are not safe; the background bearing sampler takes this
# non-blocking and simply skips a cycle rather than making a look wait.
_detector_lock_use = threading.Lock()
_shared_detector: Any = None


def get_detector() -> Any:
    """Process-wide detector, built once. None if it cannot be constructed."""
    global _shared_detector
    with _detector_lock:
        if _shared_detector is None:
            try:
                from hal.drivers.tracking.detection import ObjectDetector

                t0 = time.monotonic()
                _shared_detector = ObjectDetector()
                logger.info(
                    "[look-aim] detector built in %.0fms (reused from now on)",
                    (time.monotonic() - t0) * 1000,
                )
            except Exception as e:
                logger.warning("[look-aim] detector unavailable: %s", e)
                return None
        return _shared_detector

_abort_evt = threading.Event()


def request_abort() -> None:
    """Ask an in-flight aim to stop as soon as it can.

    Called by the physical-button single-click path: a click means "stop moving
    and pay attention to me", and an aim that kept turning through it would be
    the exact failure that gesture exists to prevent.
    """
    _abort_evt.set()


@dataclass
class AimResult:
    """Outcome of one aim attempt. Never raises — the capture must proceed."""

    aimed: bool
    reason: str
    iterations: int = 0
    yaw_moved_deg: float = 0.0
    final_dx_frac: Optional[float] = None
    # How many remembered-bearing steps were taken. Task E reads this to judge
    # whether the estimate is still predicting well.
    bearing_steps: int = 0
    # Absolute servo yaw before and after — the only way to tell from a trace
    # whether the head actually MOVED, as opposed to deciding it should have.
    start_yaw: Optional[float] = None
    end_yaw: Optional[float] = None
    # The remembered bearing as consulted this look: its value and confidence,
    # or None when nothing was stored yet. Answers "did it look where it
    # remembered, or did it have nothing to go on?"
    bearing_consulted: Optional[dict] = None
    # One entry per decision, in order. What was seen, where, what was commanded.
    steps: list = field(default_factory=list)
    # Size of the LAST correction issued. The caller scales the capture settle
    # to it: an aim that exits right after a 30 deg swing leaves the arm still
    # ringing, and a flat settle photographs the ring as motion blur.
    last_move_deg: float = 0.0


# How long after a servo write a frame is trusted to show the new pose.
# Mirrors capture_still's settle: the arm is still ringing before this. 0.25 was
# too short — a trace showed a +15.3 deg command reading back as +9.78 deg
# because the pose was sampled mid-flight, and the next step then corrected from
# an offset the head was still travelling through.
FRAME_SETTLE_S: float = 0.35
# Cap on waiting for that fresh frame — better a slightly stale measurement
# than a stalled aim.
FRAME_WAIT_S: float = 0.6


def _grab_frame(cap: Any, svc: Any = None, require_fresh: bool = False) -> Optional[Any]:
    """A frame captured AFTER the last servo write, not just the newest one held.

    This must wait. Reading `last_frame` immediately after commanding a move
    returns the PRE-move image, so the next correction is computed from a pose
    the head has already left — the loop then re-issues the same correction and
    marches the head across the room while `dx` never changes (observed on
    device: six identical +12.3 deg steps, dx frozen at 0.241, 61 deg travelled).

    Caller must hold the consumer (see `_camera_consumer`) — without one the
    device does not capture at full FPS and a fresh frame may never arrive.
    """
    try:
        # isinstance guards, not truthiness: a test double's attribute is a Mock,
        # which is truthy but not a number, and the arithmetic below would then
        # raise into the except and silently report "no frame".
        quiet_from = 0.0
        last_write = getattr(svc, "last_servo_write", 0.0) if svc is not None else 0.0
        if isinstance(last_write, (int, float)) and last_write > 0:
            quiet_from = float(last_write) + FRAME_SETTLE_S
        deadline = time.monotonic() + FRAME_WAIT_S
        while time.monotonic() < deadline:
            ts = getattr(cap, "last_frame_ts", 0.0)
            if not isinstance(ts, (int, float)):
                ts = 0.0
            if ts >= quiet_from:
                frame = cap.last_frame
                if frame is not None:
                    return frame
            time.sleep(0.03)
        if require_fresh:
            # Deliberately no best-effort fallback here. Correcting again from a
            # frame the head has already moved past is what marched it across
            # the room; with no new evidence the right move is no move.
            return None
        return cap.last_frame  # first measurement: nothing has moved yet
    except Exception as e:  # camera races are not worth failing the capture over
        logger.debug("[look-aim] frame grab failed: %s", e)
        return None


def prewarm() -> None:
    """Build the detector AND run one throwaway inference, off the critical path.

    Constructing the detector is cheap (~400ms); the FIRST detect() is not — the
    model loads and compiles lazily inside it. On device that cost a 6.0s first
    aim that blew its own 2.5s deadline and captured uncentred, while every
    later iteration ran ~0.4s. Paying it once at startup makes the first real
    look as fast as the rest.
    """
    try:
        import numpy as np

        t0 = time.monotonic()
        det = get_detector()
        blank = np.zeros((480, 640, 3), dtype=np.uint8)
        det.detect(blank, "person", strict=False)
        logger.info("[look-aim] detector prewarmed in %.0fms", (time.monotonic() - t0) * 1000.0)
    except Exception as e:
        # Never fatal: a cold detector only costs latency on the first look.
        logger.debug("[look-aim] prewarm skipped: %s", e)


@contextlib.contextmanager
def _camera_consumer(cap: Any):
    """Hold the camera at full FPS for the whole aim.

    Acquiring and releasing per frame let the device drop back to reduced
    capture between iterations, so `last_frame` went stale exactly when the loop
    needed it fresh.
    """
    held = False
    try:
        cap.acquire_consumer()
        held = True
    except Exception as e:
        logger.debug("[look-aim] consumer acquire failed: %s", e)
    try:
        yield
    finally:
        if held:
            try:
                cap.release_consumer()
            except Exception:
                pass


def _is_near_enough(box: Tuple[int, int, int, int], frame: Any, target: str) -> bool:
    """Is this box a person close enough to be the one talking to us?

    Apparent size is the only distance cue a single camera has, and the person
    who asked "look at what I'm holding" is by definition within arm's reach of
    the thing they are holding. Device frames (2026-08-19, 1280x720):

      ~22px  face  — someone across the office; the lamp chased this
      ~65px  person — a real colleague, far away, also not the asker
      ~165px person — the actual user, close, clipped by the frame edge

    Height, not area or width: a close subject is routinely clipped left/right
    (the good frame above is half out of shot) but their apparent height still
    scales with distance.
    """
    try:
        _x, _y, _w, h = box
        frame_h = float(frame.shape[0])
        if frame_h <= 0:
            return True
        floor = (
            config.LOOK_AIM_MIN_FACE_HEIGHT_FRAC if target == "face"
            else config.LOOK_AIM_MIN_PERSON_HEIGHT_FRAC
        )
        return (float(h) / frame_h) >= floor
    except Exception:
        return True  # never let the filter itself lose a subject


def _detect_subject(detector: Any, frame: Any):
    """Nearest plausible person box preferred, face as fallback.

    Person first because a hand-held object often occludes the face but rarely
    the whole body — and because framing the person includes whatever they are
    holding, which a tightly centred face does not.

    Detections too small to be the asker are rejected rather than returned: the
    caller treats "no subject" as a reason to hold or consult the remembered
    bearing, which is a far better answer than turning to a stranger at the
    other end of the room.

    Returns (box, target, confidence); confidence is None for detectors that do
    not report one.
    """
    with _detector_lock_use:
        for target in ("person", "face"):
            try:
                box = detector.detect(
                    frame, target, strict=False,
                    min_conf=config.LOOK_AIM_MIN_CONFIDENCE,
                )
            except TypeError:
                # Detector predates the min_conf parameter — the size gate below
                # still applies, so degrade rather than lose the subject.
                box = detector.detect(frame, target, strict=False)
            except Exception as e:
                logger.debug("[look-aim] detect(%s) failed: %s", target, e)
                continue
            if box is None:
                continue
            if not _is_near_enough(box, frame, target):
                logger.debug(
                    "[look-aim] ignoring far %s: box height %dpx of %dpx",
                    target, box[3], frame.shape[0],
                )
                continue
            return box, target, getattr(detector, "last_confidence", None)
    return None, "", None


@contextlib.contextmanager
def servo_ownership():
    """Own the body for one aim + capture, so nothing else can move the head.

    Without this the sequence "hear the question -> aim -> capture" can be
    broken by an emotion animation landing in the middle: emotion presets play
    RECORDED poses that are absolute on every joint, including wrist_roll, so
    one arriving between the aim and the shutter re-poses the head entirely and
    the frame shows wherever the animation parked it.

    `nudge()` already preempts an animation that is ALREADY playing, but not one
    dispatched afterwards — which is exactly the window the capture sits in.

    `_tracking_active` is the existing lock for this: routes/emotion.py
    suppresses ALL emotion servo while it is set, and the animation loop drops
    any in-progress recording rather than fighting for the joints. The vision
    tracker uses the same flag.

    The previous value is restored rather than cleared, so this never releases a
    genuine tracking session that was already running.
    """
    svc = None
    prev = False
    try:
        import hal.app_state as state

        svc = getattr(state, "animation_service", None)
        if svc is not None:
            prev = getattr(svc, "_tracking_active", False)
            svc._tracking_active = True
    except Exception as e:  # never block a capture over the lock
        logger.debug("[look-aim] servo ownership unavailable: %s", e)
        svc = None
    try:
        yield
    finally:
        if svc is not None:
            try:
                svc._tracking_active = prev
            except Exception as e:
                logger.warning("[look-aim] failed to release servo ownership: %s", e)


def _say(pool: str) -> None:
    """Speak one phrase from a named filler pool, best-effort.

    os-server owns the phrases, the language and the WAV cache; HAL only decides
    WHEN. Fire-and-forget: the aim must never wait on speech, and a muted speaker
    is handled downstream by the speak path.
    """
    try:
        import requests

        requests.post(config.OS_SENSING_FILLER_URL, json={"pool": pool}, timeout=1.0)
    except Exception as e:
        logger.debug("[look-aim] filler '%s' skipped: %s", pool, e)


def _yaw_of(svc: Any) -> Optional[float]:
    try:
        return round(float(svc.get_positions().get("base_yaw.pos", 0.0)), 2)
    except Exception:
        return None


def _note_sighting(svc: Any) -> None:
    """Remember that a subject was confirmed at the current pose."""
    global _last_seen_mono, _last_seen_yaw
    try:
        _last_seen_yaw = float(svc.get_positions().get("base_yaw.pos", 0.0))
    except Exception:
        return
    _last_seen_mono = time.monotonic()


def _recently_seen_here(svc: Any) -> bool:
    """True when a subject was confirmed at roughly this pose moments ago.

    Then a sudden disappearance means OCCLUSION, not absence — hold and capture
    rather than turning away from whatever is being held up to the camera.
    """
    if _last_seen_mono <= 0.0:
        return False
    if time.monotonic() - _last_seen_mono > RECENT_SIGHTING_S:
        return False
    try:
        yaw = float(svc.get_positions().get("base_yaw.pos", 0.0))
    except Exception:
        return False
    return abs(yaw - _last_seen_yaw) <= RECENT_SIGHTING_YAW_TOL_DEG


def _step_toward_bearing(svc: Any, out: Optional[dict] = None) -> bool:
    """Take ONE bounded step toward the remembered bearing. True if it moved.

    `out` collects what the estimate said, so a trace can distinguish "there was
    no bearing to use" from "there was one and it was wrong".
    """
    try:
        from hal.drivers.tracking import user_bearing
        import hal.app_state as state

        est = user_bearing.read_estimate()
        if out is not None:
            # Built defensively and separately: describing the estimate must
            # never be able to stop the lamp from USING it.
            try:
                out["bearing"] = None if est is None else {
                    "bearing_deg": getattr(est, "bearing_deg", None),
                    "confidence": getattr(est, "confidence", None),
                    "samples": getattr(est, "samples", None),
                    "age_s": getattr(est, "age_s", None),
                }
            except Exception:
                out["bearing"] = None
        if est is None:
            if out is not None:
                out["skipped"] = "no bearing recorded yet"
            return False
        if est.confidence < MIN_BEARING_CONFIDENCE:
            if out is not None:
                out["skipped"] = f"confidence {est.confidence:.2f} < {MIN_BEARING_CONFIDENCE}"
            return False
        current = svc.get_positions()
        target, step = _bearing_step_target(svc, est, current)
        if target is None:
            return False  # already in the remembered shape; nothing left to try
        # Absolute move, not a relative nudge. The pitch SIGN was never validated
        # for the nudge path (see this module's docstring), which is why the aim
        # has only ever driven yaw. An absolute target has no sign to get wrong,
        # so restoring the remembered posture is safe here where correcting it
        # incrementally would not be.
        duration = min_move_duration(
            state.safety_policy, target, current, MOVE_DURATION_S
        )
        svc.move_and_hold(target, duration=duration)
        logger.info(
            "[look-aim] no subject — stepping %+.1f deg toward remembered bearing "
            "%+.1f (conf=%.2f, restoring %d joints)",
            step, est.bearing_deg, est.confidence, len(target),
        )
        return True
    except Exception as e:
        logger.debug("[look-aim] bearing step skipped: %s", e)
        return False


# Self-calibration bounds for the measured degrees-per-dx_frac scale. The
# device has measured 91 deg near the frame centre and 229 deg at the edge, so
# the window is deliberately wide — it exists only to reject nonsense from a
# noisy or mis-detected step, not to encode a value.
MIN_SCALE_DEG: float = 40.0
# 400 was too permissive: the device measured 302 at the frame edge, which asked
# for a 70 deg correction, got clamped to 45, and overshot the subject.
MAX_SCALE_DEG: float = 250.0
# The measured scale is taken at the CURRENT eccentricity but spent on the NEXT,
# smaller one — and on a fisheye the scale shrinks toward the centre, so the
# measurement is systematically too big for the correction it is used for.
# Biasing low costs an extra step at worst; biasing high oscillates, which is
# what dx=+27% -> -10% -> +11% looked like on device.
SCALE_SAFETY: float = 0.7
# Below these, a step tells us nothing: dividing a tiny shift by a tiny move
# amplifies detector jitter into a wild scale.
CALIB_MIN_MOVE_DEG: float = 3.0
CALIB_MIN_SHIFT_FRAC: float = 0.02
# How fast the measured scale follows the newest step. High because the true
# scale genuinely changes as the subject moves in from the edge.
CALIB_ALPHA: float = 0.6
# Hard cap on one correction, whatever the measured scale says.
MAX_STEP_DEG: float = 45.0


def _measure_scale(moved_deg: float, shift_frac: float) -> Optional[float]:
    """Degrees of yaw per unit dx_frac, measured from what the last step did.

    This replaces the fixed FOV constant, which cannot be right everywhere: the
    lens is a fisheye, so degrees-per-pixel grows toward the edge (device-
    measured 91 deg centre, 229 deg edge). One constant either overshoots the
    middle or crawls at the edge — measuring the LOCAL scale each step avoids
    choosing.

    Returns None when the step is not informative enough to divide by.
    """
    if abs(moved_deg) < CALIB_MIN_MOVE_DEG or abs(shift_frac) < CALIB_MIN_SHIFT_FRAC:
        return None
    if (moved_deg > 0) != (shift_frac > 0):
        # The subject moved the wrong way for our correction — they walked, or
        # the detector jumped to something else. Not a measurement of optics.
        return None
    scale = abs(moved_deg) / abs(shift_frac)
    if not (MIN_SCALE_DEG <= scale <= MAX_SCALE_DEG):
        return None
    return scale


# A joint is "already there" within this much, so a search does not re-issue a
# move for rounding noise on every step.
POSE_TOLERANCE_DEG: float = 2.0


def _bearing_step_target(svc: Any, est: Any, current: dict):
    """Absolute pose for one bearing step: yaw stepped, the rest restored.

    Returns (target, step_deg), or (None, 0.0) when the head is already in the
    remembered shape.

    The move goes straight to the remembered pose. It used to advance in
    BEARING_STEP_DEG hops, re-detecting between them so it could not sail past
    someone standing en route — but the lens sees about 110 deg, so anyone
    between here and there is already in frame before the head moves at all.
    The hops bought no extra coverage and cost a detect plus a settle each,
    which is most of a second per hop against the aim's deadline.
    """
    cur_yaw = float(current.get("base_yaw.pos", 0.0))
    delta = float(est.bearing_deg) - cur_yaw
    step = delta

    try:
        valid = set(svc.get_joint_names())
    except Exception:
        valid = set(current.keys())

    # isinstance, not truthiness: a malformed estimate (or a test double) can
    # carry a non-dict here, and iterating it would raise inside the caller's
    # except and silently disable the whole bearing step.
    pose = getattr(est, "pose", None)
    if not isinstance(pose, dict):
        pose = {}

    target: dict = {}
    for joint, value in pose.items():
        if joint == "base_yaw.pos" or joint not in valid:
            continue
        if abs(float(value) - float(current.get(joint, value))) > POSE_TOLERANCE_DEG:
            target[joint] = float(value)

    # Yaw still to travel, or only the posture is wrong (head left pointing at
    # the floor at the right bearing — the case that made a correct search
    # sweep the ground and find nobody).
    if abs(delta) >= 1.0:
        target["base_yaw.pos"] = cur_yaw + step
    elif not target:
        return None, 0.0
    return target, step


def _score_prediction(bearing_steps: int, found: bool) -> None:
    """Tell the estimate whether turning to it actually found anyone.

    Only meaningful when we actually turned to the bearing — an aim that never
    consulted it says nothing about whether it is still right.
    """
    if bearing_steps <= 0:
        return
    try:
        from hal.drivers.tracking import user_bearing

        user_bearing.record_prediction(found)
    except Exception as e:
        logger.debug("[look-aim] prediction scoring skipped: %s", e)


def _record_bearing_if_centred(svc: Any, dx_frac: float) -> None:
    """Fold this sighting into the remembered bearing, if it is centred enough.

    Passive by design: nothing reads the estimate yet. Failures are swallowed —
    losing a sample must never cost the user their answer.
    """
    if abs(dx_frac) > RECORD_DEADBAND_FRAC:
        return
    try:
        from hal.drivers.tracking import user_bearing

        positions = svc.get_positions()
        yaw = float(positions.get("base_yaw.pos", 0.0))
        # The whole shape, not just the base: pitch lives across base/elbow/wrist,
        # so yaw alone cannot describe "looking at the user".
        user_bearing.record_sighting(yaw, pose=positions)
    except Exception as e:
        logger.debug("[look-aim] bearing record skipped: %s", e)


def aim_for_look(deadline_s: float, detector: Any = None) -> AimResult:
    """Centre the subject in yaw, then return so the caller can capture.

    Always returns — a failed aim must still let `look` capture something,
    because dead air is worse than an imperfectly framed frame.
    """
    import hal.app_state as state

    _abort_evt.clear()
    t_end = time.monotonic() + max(0.0, deadline_s)

    cap = getattr(state, "camera_capture", None)
    svc = getattr(state, "animation_service", None)
    if cap is None or svc is None:
        return AimResult(False, "no camera or animation service")
    if getattr(state, "_camera_disabled", False):
        # Privacy: never move toward someone who has asked the device not to look.
        return AimResult(False, "camera disabled")

    if detector is None:
        detector = get_detector()
        if detector is None:
            return AimResult(False, "no detector")

    iterations = 0
    yaw_total = 0.0
    bearing_steps = 0
    last_dx_frac: Optional[float] = None
    # Measured degrees-per-dx_frac for THIS aim. Per-aim, not persisted: the
    # scale depends on where in the frame the subject is, so a value learned
    # last time at the edge would be wrong near the centre.
    scale_deg: Optional[float] = None
    # (yaw, dx_frac) captured just before the last move, so the next
    # measurement can tell what that move actually achieved.
    pending_calib: Optional[Tuple[float, float]] = None
    last_move_deg = 0.0
    announced_found = False
    start_yaw = _yaw_of(svc)
    steps: list = []
    bearing_consulted: Optional[dict] = None

    def _result(aimed: bool, reason: str) -> AimResult:
        """Build the outcome with the pose actually reached, so a trace shows
        whether the head moved rather than just what was decided."""
        return AimResult(
            aimed, reason, iterations, yaw_total, last_dx_frac, bearing_steps,
            start_yaw, _yaw_of(svc), bearing_consulted, steps, last_move_deg,
        )

    with _camera_consumer(cap):
        while iterations < MAX_ITERATIONS:
            if _abort_evt.is_set():
                return _result(False, "aborted")
            if time.monotonic() >= t_end:
                return _result(False, "deadline")

            with look_debug.stage("aim.frame_wait"):
                frame = _grab_frame(cap, svc, require_fresh=iterations > 0)
            if frame is None:
                if iterations > 0:
                    # Aim on what we have rather than steering blind.
                    return _result(
                        abs(last_dx_frac or 1.0) <= CENTRE_DEADBAND_FRAC, "no fresh frame"
                    )
                return _result(False, "no frame")

            with look_debug.stage("aim.detect"):
                box, kind, conf = _detect_subject(detector, frame)
            if box is None:
                # Log the empty frame too — "what did it see when it saw nothing"
                # is exactly the question a hold/search step raises.
                look_debug.note_step_frame(
                    iterations + 1, frame, None, f"iter {iterations + 1}: no detection"
                )
                # Priority 2 — occlusion, not absence. Never turn away from a scene
                # that just changed dramatically: a large object filling the frame is
                # evidence the subject is right there.
                if _recently_seen_here(svc):
                    steps.append({"n": iterations + 1, "saw": None,
                                  "action": "hold (recent sighting — likely occluded)",
                                  "yaw": _yaw_of(svc)})
                    return _result(False, "holding: seen here moments ago (likely occluded)")
                # Priority 3 — step toward the remembered bearing, re-detecting after
                # every step so we cannot sail past someone en route.
                probe: dict = {}
                if bearing_steps < MAX_BEARING_STEPS and _step_toward_bearing(svc, probe):
                    bearing_consulted = probe.get("bearing")
                    steps.append({"n": iterations + 1, "saw": None,
                                  "action": "step toward remembered bearing",
                                  "bearing": probe.get("bearing"), "yaw": _yaw_of(svc)})
                    if bearing_steps == 0 and config.LOOK_AIM_SPEAK:
                        # Only on the FIRST step: the lamp is about to turn away from
                        # the user mid-question, which reads as broken unless explained.
                        _say("look_searching")
                    bearing_steps += 1
                    iterations += 1
                    continue
                _score_prediction(bearing_steps, found=False)
                bearing_consulted = probe.get("bearing", bearing_consulted)
                steps.append({"n": iterations + 1, "saw": None,
                              "action": probe.get("skipped", "give up — nothing found"),
                              "yaw": _yaw_of(svc)})
                return _result(False, "subject not found")

            if bearing_steps > 0 and not announced_found and config.LOOK_AIM_SPEAK:
                # Once, and only after a search was announced. `bearing_steps`
                # stays above zero for the REST of the aim, so without this latch
                # every subsequent centring iteration re-announced it — device
                # 2026-08-19 said "bạn đây rồi" four times in three seconds.
                announced_found = True
                _say("look_found")
            _score_prediction(bearing_steps, found=True)
            _note_sighting(svc)
            x, _y, w, _h = box
            w_fr = float(frame.shape[1])
            dx = (x + w / 2.0) - (w_fr / 2.0)
            last_dx_frac = dx / w_fr
            conf_txt = f" conf={conf:.2f}" if isinstance(conf, (int, float)) else ""
            look_debug.note_step_frame(
                iterations + 1, frame, box,
                f"iter {iterations + 1}: {kind}{conf_txt} dx={last_dx_frac * 100:+.1f}%",
            )

            # Learn the local scale from what the previous step actually did,
            # before deciding whether we are done.
            if pending_calib is not None:
                prev_yaw, prev_dx = pending_calib
                measured = _measure_scale(
                    _yaw_of(svc) - prev_yaw, prev_dx - last_dx_frac
                )
                if measured is not None:
                    scale_deg = (
                        measured if scale_deg is None
                        else (1.0 - CALIB_ALPHA) * scale_deg + CALIB_ALPHA * measured
                    )
                pending_calib = None

            if abs(last_dx_frac) <= CENTRE_DEADBAND_FRAC:
                _record_bearing_if_centred(svc, last_dx_frac)
                return _result(True, f"centred on {kind}")

            # Yaw sign per the tracker's verified convention: dx>0 (subject right of
            # centre) -> base_yaw INCREASES. Do not flip this without device evidence.
            # Magnitude comes from the MEASURED scale once we have one; the config
            # FOV is only the first-step guess.
            scale = (
                scale_deg * SCALE_SAFETY if scale_deg is not None
                else config.LOOK_AIM_FOV_DEG
            )
            yaw_deg = AIM_GAIN * last_dx_frac * scale
            yaw_deg = max(-MAX_STEP_DEG, min(MAX_STEP_DEG, yaw_deg))
            pending_calib = (_yaw_of(svc), last_dx_frac)

            try:
                with look_debug.stage("aim.move"):
                    current = svc.get_positions()
                    svc.nudge(yaw_deg, 0.0, MOVE_DURATION_S, current, state.safety_policy)
            except Exception as e:
                logger.warning("[look-aim] nudge failed: %s", e)
                return _result(False, f"nudge failed: {e}")

            yaw_total += yaw_deg
            last_move_deg = yaw_deg
            iterations += 1
            steps.append({"n": iterations, "saw": kind, "dx_frac": round(last_dx_frac, 3),
                          "conf": round(conf, 3) if isinstance(conf, (int, float)) else None,
                          "action": f"centre: yaw {yaw_deg:+.1f}", "yaw": _yaw_of(svc),
                          "scale": round(scale, 1)})
            logger.info(
                "[look-aim] iter=%d %s dx=%.0fpx (%.1f%%) -> yaw %+.1f deg (scale=%.0f%s)",
                iterations, kind, dx, last_dx_frac * 100.0, yaw_deg, scale,
                "" if scale_deg is not None else " guess",
            )

    return _result(abs(last_dx_frac or 1.0) <= CENTRE_DEADBAND_FRAC, "max iterations")
