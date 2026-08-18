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

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional, Tuple

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


def _step_toward_bearing(svc: Any) -> bool:
    """Take ONE bounded step toward the remembered bearing. True if it moved."""
    try:
        from hal.drivers.tracking import user_bearing
        import hal.app_state as state

        est = user_bearing.read_estimate()
        if est is None or est.confidence < MIN_BEARING_CONFIDENCE:
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
        from hal.drivers.tracking.detection import ObjectDetector

        detector = ObjectDetector()

    iterations = 0
    yaw_total = 0.0
    bearing_steps = 0
    last_dx_frac: Optional[float] = None

    while iterations < MAX_ITERATIONS:
        if _abort_evt.is_set():
            return AimResult(False, "aborted", iterations, yaw_total, last_dx_frac, bearing_steps)
        if time.monotonic() >= t_end:
            return AimResult(False, "deadline", iterations, yaw_total, last_dx_frac, bearing_steps)

        frame = _grab_frame(cap)
        if frame is None:
            return AimResult(False, "no frame", iterations, yaw_total, last_dx_frac, bearing_steps)

        box, kind = _detect_subject(detector, frame)
        if box is None:
            # Priority 2 — occlusion, not absence. Never turn away from a scene
            # that just changed dramatically: a large object filling the frame is
            # evidence the subject is right there.
            if _recently_seen_here(svc):
                return AimResult(False, "holding: seen here moments ago (likely occluded)",
                                 iterations, yaw_total, last_dx_frac, bearing_steps)
            # Priority 3 — step toward the remembered bearing, re-detecting after
            # every step so we cannot sail past someone en route.
            if bearing_steps < MAX_BEARING_STEPS and _step_toward_bearing(svc):
                bearing_steps += 1
                iterations += 1
                continue
            return AimResult(False, "subject not found", iterations, yaw_total,
                             last_dx_frac, bearing_steps)

        _note_sighting(svc)
        x, _y, w, _h = box
        w_fr = float(frame.shape[1])
        dx = (x + w / 2.0) - (w_fr / 2.0)
        last_dx_frac = dx / w_fr

        if abs(last_dx_frac) <= CENTRE_DEADBAND_FRAC:
            _record_bearing_if_centred(svc, last_dx_frac)
            return AimResult(True, f"centred on {kind}", iterations, yaw_total, last_dx_frac, bearing_steps)

        # Yaw sign per the tracker's verified convention: dx>0 (subject right of
        # centre) -> base_yaw INCREASES. Do not flip this without device evidence.
        yaw_deg = AIM_GAIN * dx * (C.CAMERA_FOV_DEG / w_fr)

        try:
            current = svc.get_positions()
            svc.nudge(yaw_deg, 0.0, MOVE_DURATION_S, current, state.safety_policy)
        except Exception as e:
            logger.warning("[look-aim] nudge failed: %s", e)
            return AimResult(False, f"nudge failed: {e}", iterations, yaw_total, last_dx_frac, bearing_steps)

        yaw_total += yaw_deg
        iterations += 1
        logger.info(
            "[look-aim] iter=%d %s dx=%.0fpx (%.1f%%) -> yaw %+.1f deg",
            iterations, kind, dx, last_dx_frac * 100.0, yaw_deg,
        )

    return AimResult(
        abs(last_dx_frac or 1.0) <= CENTRE_DEADBAND_FRAC,
        "max iterations",
        iterations,
        yaw_total,
        last_dx_frac,
        bearing_steps,
    )
