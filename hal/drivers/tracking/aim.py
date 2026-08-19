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


def _grab_frame(cap: Any) -> Optional[Any]:
    """Latest camera frame, cheaply. NOT capture_still: that freezes the servos
    and waits for the arm to settle, which is right for the shutter but wrong
    inside a control loop that is deliberately moving the arm."""
    try:
        cap.acquire_consumer()
        try:
            return cap.last_frame
        finally:
            cap.release_consumer()
    except Exception as e:  # camera races are not worth failing the capture over
        logger.debug("[look-aim] frame grab failed: %s", e)
        return None


def _detect_subject(detector: Any, frame: Any) -> Tuple[Optional[Tuple[int, int, int, int]], str]:
    """Person box preferred, face as fallback.

    Person first because a hand-held object often occludes the face but rarely
    the whole body — and because framing the person includes whatever they are
    holding, which a tightly centred face does not.
    """
    for target in ("person", "face"):
        try:
            box = detector.detect(frame, target, strict=False)
        except Exception as e:
            logger.debug("[look-aim] detect(%s) failed: %s", target, e)
            continue
        if box is not None:
            return box, target
    return None, ""


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
        delta = est.bearing_deg - float(current.get("base_yaw.pos", 0.0))
        if abs(delta) < 1.0:
            return False  # already pointing there; nothing left to try
        step = max(-BEARING_STEP_DEG, min(BEARING_STEP_DEG, delta))
        svc.nudge(step, 0.0, MOVE_DURATION_S, current, state.safety_policy)
        logger.info(
            "[look-aim] no subject — stepping %+.1f deg toward remembered bearing "
            "%+.1f (conf=%.2f)", step, est.bearing_deg, est.confidence,
        )
        return True
    except Exception as e:
        logger.debug("[look-aim] bearing step skipped: %s", e)
        return False


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

        yaw = float(svc.get_positions().get("base_yaw.pos", 0.0))
        user_bearing.record_sighting(yaw)
    except Exception as e:
        logger.debug("[look-aim] bearing record skipped: %s", e)


def aim_for_look(deadline_s: float, detector: Any = None) -> AimResult:
    """Centre the subject in yaw, then return so the caller can capture.

    Always returns — a failed aim must still let `look` capture something,
    because dead air is worse than an imperfectly framed frame.
    """
    import hal.app_state as state
    from hal.drivers.tracking import constants as C

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
    start_yaw = _yaw_of(svc)
    steps: list = []
    bearing_consulted: Optional[dict] = None

    def _result(aimed: bool, reason: str) -> AimResult:
        """Build the outcome with the pose actually reached, so a trace shows
        whether the head moved rather than just what was decided."""
        return AimResult(
            aimed, reason, iterations, yaw_total, last_dx_frac, bearing_steps,
            start_yaw, _yaw_of(svc), bearing_consulted, steps,
        )

    while iterations < MAX_ITERATIONS:
        if _abort_evt.is_set():
            return _result(False, "aborted")
        if time.monotonic() >= t_end:
            return _result(False, "deadline")

        frame = _grab_frame(cap)
        if frame is None:
            return _result(False, "no frame")

        box, kind = _detect_subject(detector, frame)
        if box is None:
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

        if bearing_steps > 0 and config.LOOK_AIM_SPEAK:
            # Only after a search was announced — otherwise "there you are" fires
            # on every visual question, which is noise.
            _say("look_found")
        _score_prediction(bearing_steps, found=True)
        _note_sighting(svc)
        x, _y, w, _h = box
        w_fr = float(frame.shape[1])
        dx = (x + w / 2.0) - (w_fr / 2.0)
        last_dx_frac = dx / w_fr

        if abs(last_dx_frac) <= CENTRE_DEADBAND_FRAC:
            _record_bearing_if_centred(svc, last_dx_frac)
            return _result(True, f"centred on {kind}")

        # Yaw sign per the tracker's verified convention: dx>0 (subject right of
        # centre) -> base_yaw INCREASES. Do not flip this without device evidence.
        yaw_deg = AIM_GAIN * dx * (C.CAMERA_FOV_DEG / w_fr)

        try:
            current = svc.get_positions()
            svc.nudge(yaw_deg, 0.0, MOVE_DURATION_S, current, state.safety_policy)
        except Exception as e:
            logger.warning("[look-aim] nudge failed: %s", e)
            return _result(False, f"nudge failed: {e}")

        yaw_total += yaw_deg
        iterations += 1
        steps.append({"n": iterations, "saw": kind, "dx_frac": round(last_dx_frac, 3),
                      "action": f"centre: yaw {yaw_deg:+.1f}", "yaw": _yaw_of(svc)})
        logger.info(
            "[look-aim] iter=%d %s dx=%.0fpx (%.1f%%) -> yaw %+.1f deg",
            iterations, kind, dx, last_dx_frac * 100.0, yaw_deg,
        )

    return _result(abs(last_dx_frac or 1.0) <= CENTRE_DEADBAND_FRAC, "max iterations")
