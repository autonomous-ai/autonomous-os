"""Turning toward the lamp as a way of addressing it.

A wake phrase and a button both make the user operate a device. Between two
people the cue for "I am talking to you" is neither: you turn toward someone and
speak. On a desk lamp — an arm's length away, in view all day — that gesture is
available in a way it is not for a speaker across a living room, which is why
copying "hey <name>" from products with no camera fits this device badly.

So this is a THIRD opener of the existing wake gate, next to the spoken wake
phrase and the single click. It opens the same window, by calling the same
seam the button uses. Nothing downstream of the gate changes.

Two design points worth keeping straight:

  * ORDER. People turn BEFORE they speak, never after. Waiting for the mic and
    then looking would arrive after the gesture is over, and — worse — could
    never see the TRANSITION from looking away to looking here, which is the
    whole signal. So the camera samples continuously into a short ring buffer
    and speech merely triggers a read BACKWARDS through it. This mirrors what
    the mic already does with its own pre-roll: it records into a lookback and
    recovers the ~640 ms before the trigger, or it would lose the start of every
    sentence.

  * PRESENCE IS NOT THE SIGNAL. The user sits beside this lamp all day, so "a
    person is visible" is true almost always and gates nothing. "A face is
    visible" is barely better — a face turned to a monitor still detects. The
    signal is head ORIENTATION, and the acceptance cone has to be tight enough
    to reject the common posture of talking to a colleague with the torso still
    square to the desk.

Head yaw is estimated from the five landmarks YuNet already returns, so no
second model is loaded and no extra inference is run.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from typing import Any, Deque, List, Optional, Sequence, Tuple

import hal.config as config

logger = logging.getLogger(__name__)

_stop = threading.Event()
_thread: Optional[threading.Thread] = None

# (monotonic timestamp, |head yaw| in degrees, face height in pixels,
#  |horizontal offset of the face from frame centre| as a fraction, 0 at the
#  centre and 1 at the edge).
# Bounded by time in _prune, not by length, so the sample rate can change
# without the window silently changing with it.
_samples: Deque[Tuple[float, float, float, float]] = deque()
_samples_lock = threading.Lock()

# perf_counter of the last gate this opened, for the cooldown.
_last_grant_t: float = 0.0
# When a usable face was last seen, and when we last turned to look for one.
_last_face_t: float = 0.0
_last_repoint_t: float = 0.0


def head_yaw_deg(landmarks: Sequence[float]) -> Optional[float]:
    """Absolute head yaw in degrees from YuNet's five landmarks.

    ``landmarks`` is the flat ``[x1, y1, ... x5, y5]`` block YuNet returns:
    right eye, left eye, nose tip, right mouth corner, left mouth corner.

    The estimate is the nose's sideways offset from the midpoint of the eyes,
    measured ALONG THE EYE LINE and normalised by half the inter-ocular
    distance. Under a pinhole projection that ratio is sin(yaw), so the angle
    comes back through asin: 0 when the nose sits centred between the eyes,
    rising to 90 as the face turns to profile and the nose reaches one eye.

    Projecting onto the eye line rather than onto the image x-axis is what makes
    this survive head ROLL — a tilted head would otherwise read as turned.

    Returns None when the geometry cannot support an estimate (eyes coincident,
    landmarks missing or non-finite). Sign is discarded deliberately: left and
    right are equally "not facing the lamp", and the caller only ever compares
    against a symmetric cone.
    """
    try:
        pts = [float(v) for v in landmarks[:10]]
    except (TypeError, ValueError):
        return None
    if len(pts) < 10 or not all(math.isfinite(v) for v in pts):
        return None

    (rx, ry), (lx, ly), (nx, ny) = (pts[0], pts[1]), (pts[2], pts[3]), (pts[4], pts[5])

    # Eye line, and the inter-ocular distance that normalises away face size and
    # distance from the camera.
    ex, ey = lx - rx, ly - ry
    eye_dist = math.hypot(ex, ey)
    if eye_dist < 1e-6:
        return None

    mid_x, mid_y = (rx + lx) / 2.0, (ry + ly) / 2.0
    # Component of (nose - eye midpoint) along the unit eye vector.
    offset = ((nx - mid_x) * ex + (ny - mid_y) * ey) / eye_dist

    ratio = offset / (eye_dist / 2.0)
    # Beyond the eyes the model has nothing left to say; clamp rather than let
    # asin raise on a landmark that drifted past its own eye.
    ratio = max(-1.0, min(1.0, ratio))
    return abs(math.degrees(math.asin(ratio)))


def cone_for(edge_frac: float) -> float:
    """The acceptance cone that applies to a face this far off frame centre.

    Widens toward the edge because the lens is not a pinhole there — see
    GAZE_EDGE_CONE_SCALE. Compensating the threshold rather than the angle is
    deliberate: undistorting properly needs calibration coefficients this device
    does not have, and a wrong correction applied to every sample would be worse
    than a known, bounded slackening applied only where the error lives.
    """
    edge = max(0.0, min(1.0, edge_frac))
    scale = 1.0 + (max(1.0, config.GAZE_EDGE_CONE_SCALE) - 1.0) * edge
    return config.GAZE_MAX_YAW_DEG * scale


def facing_lamp(yaw_deg: Optional[float], face_px: float,
                edge_frac: float = 0.0) -> bool:
    """Whether one sample counts as the user facing the lamp.

    The size test comes first in spirit: below GAZE_MIN_FACE_PX the yaw is not a
    weak measurement, it is not a measurement at all, so it must never vote —
    not even to say "away".
    """
    if yaw_deg is None:
        return False
    return (
        face_px >= config.GAZE_MIN_FACE_PX
        and yaw_deg <= cone_for(edge_frac)
    )


def _prune(now: float) -> None:
    """Drop samples older than the buffer window. Caller holds the lock."""
    cutoff = now - max(1.0, config.GAZE_BUFFER_S)
    while _samples and _samples[0][0] < cutoff:
        _samples.popleft()


def record_sample(yaw_deg: Optional[float], face_px: float,
                  edge_frac: float = 0.0, now: Optional[float] = None) -> None:
    """Append one observation to the ring buffer."""
    t = time.monotonic() if now is None else now
    with _samples_lock:
        # A frame with no usable face is recorded as "not facing", not dropped:
        # a gap in the record is evidence against a continuous hold, and
        # silently skipping it would let a hold survive the user leaving.
        _samples.append(
            (t, float("inf") if yaw_deg is None else yaw_deg, face_px, edge_frac)
        )
        _prune(t)


def snapshot() -> List[Tuple[float, float, float, float]]:
    with _samples_lock:
        return list(_samples)


def facing_ratio(now: Optional[float] = None) -> Tuple[float, int]:
    """``(fraction facing, samples examined)`` over the decision window.

    A RATIO, not an unbroken run. Per-sample yaw carries real measurement noise
    — see GAZE_MIN_FACING_RATIO for the device trail that settled this — so any
    rule demanding every sample pass rejects users who are plainly facing the
    lamp. A majority separates the two populations with room to spare.

    The window is the GAZE_WINDOW_S immediately before ``now`` — the span
    between turning toward the lamp and starting to speak. Speech is the
    trigger; the gesture it is evidence for happened just before it, never
    after, which is why nothing later than the trigger is ever consulted.
    """
    t = time.monotonic() if now is None else now
    samples = snapshot()
    if not samples:
        return 0.0, 0

    window = max(0.0, config.GAZE_WINDOW_S)
    # Nothing recent enough to describe the present — the watcher was blocked,
    # or the camera stopped producing.
    if (t - samples[-1][0]) > window:
        return 0.0, 0

    # Only samples that actually MEASURED a head can vote, either way.
    #
    # A frame with no usable face is not evidence of a head turned away, it is
    # no evidence at all, and counting it as a vote against punishes the user
    # for the detector blinking. Device-measured: a user sitting still with a
    # 93 px face, well centred, produced [32,11,-,-,-,34,38,24] and scored 50%
    # against a 60% bar — refused for three frames the detector dropped, not for
    # anything they did.
    #
    # Losing sight of someone entirely is still handled, just not by this ratio:
    # the denominator shrinks below GAZE_MIN_SAMPLES and the caller declines to
    # decide, which is the honest answer when nothing was seen.
    measured = [
        s for s in samples
        if s[0] >= t - window and s[1] != float("inf") and s[2] >= config.GAZE_MIN_FACE_PX
    ]
    if not measured:
        return 0.0, 0
    facing = sum(1 for _, yaw, px, edge in measured if facing_lamp(yaw, px, edge))
    return facing / float(len(measured)), len(measured)


_skips_logged: set = set()


def _skip(reason: str) -> str:
    """Log the FIRST time sampling is skipped for each distinct reason.

    A watcher that quietly records nothing looks identical to a user who is
    never there, and the difference only shows up as a gate that never opens.
    Logging once per reason names the cause without a line every 300 ms.
    """
    if reason not in _skips_logged:
        _skips_logged.add(reason)
        logger.info("[gaze] not sampling: %s (logged once)", reason)
    return reason


def _sample_once() -> Optional[str]:
    """One camera observation folded into the buffer.

    Returns None when a sample was recorded, else the reason it was skipped.
    """
    import hal.app_state as state

    from hal.drivers.tracking import aim, detection, frame_utils

    if getattr(state, "_camera_disabled", False):
        return _skip("camera disabled for privacy")
    cap = getattr(state, "camera_capture", None)
    svc = getattr(state, "animation_service", None)
    if cap is None:
        return _skip("no camera capture on this device")
    if svc is None:
        return _skip("no animation service")
    # Mid-aim or mid-track the head is swinging, so a yaw measured now describes
    # the lamp's motion rather than the user's intent.
    if getattr(svc, "_tracking_active", False):
        return "body busy aiming or tracking"

    # Non-blocking: a live look must never wait behind a background sample.
    if not aim._detector_lock_use.acquire(blocking=False):
        return "detector busy with a live look"
    try:
        # svc deliberately NOT passed. _grab_frame's settle logic exists for the
        # aim, which must not correct from a pre-move frame, so it waits for a
        # frame captured after `last_servo_write`. Idle animations write servos
        # constantly, so passing svc here made every sample wait out a settle it
        # has no use for — measured as 1.3 samples/s against the 3/s configured.
        # This loop never moves anything; the newest frame is always valid.
        frame = aim._grab_frame(cap, None)
        if frame is None:
            return _skip("no frame from the camera")
        # Detect on a downscaled copy. At 720p this loop measured ~1.5 samples/s
        # against the 3/s it asks for, which starves the hold check of the two
        # or three observations it needs. Head ORIENTATION survives shrinking —
        # the landmarks move together and the yaw ratio is scale-invariant — so
        # the resolution buys nothing here, unlike a bbox the aim must centre.
        small, _ = frame_utils.downscale(frame)
        face = detection.detect_face_with_landmarks(small)
    finally:
        aim._detector_lock_use.release()

    if face is None:
        record_sample(None, 0.0, 0.0)
        return None
    (fx, _, fw, fh), landmarks = face
    frame_w = float(small.shape[1]) or 1.0
    # Distance of the face centre from the frame centre, 0 at the middle and 1
    # at either edge — how far into the lens distortion this sample sits.
    edge = min(1.0, abs((fx + fw / 2.0) - frame_w / 2.0) / (frame_w / 2.0))
    # Height of the face as DETECTED, i.e. in the downscaled frame the landmarks
    # were measured in — that is the resolution the yaw precision depends on.
    global _last_face_t
    if float(fh) >= config.GAZE_MIN_FACE_PX and edge <= config.GAZE_WELL_FRAMED_EDGE:
        # Big enough to measure AND not about to leave the frame. Both halves
        # matter: background colleagues fail the size floor, and a user drifting
        # to the edge used to keep resetting this clock while sliding out of
        # view, so the lamp never turned to keep them.
        _last_face_t = time.monotonic()
    record_sample(head_yaw_deg(landmarks), float(fh), edge)
    return None


def on_speech_start() -> bool:
    """Called the moment VAD confirms speech. Decides, logs, maybe opens the gate.

    Returns True when the gate was actually opened, which is never in shadow
    mode. Never raises: a failure here must degrade to today's behaviour (wake
    phrase and button only), not break the voice loop.
    """
    global _last_grant_t
    try:
        if not config.GAZE_WAKE_ENABLED:
            return False

        ratio, considered = facing_ratio()
        samples = snapshot()
        latest = samples[-1] if samples else None
        yaw_txt = "none" if latest is None or latest[1] == float("inf") else f"{latest[1]:.1f}"
        px_txt = "0" if latest is None else f"{latest[2]:.0f}"
        edge_txt = "0.00" if latest is None else f"{latest[3]:.2f}"
        would = (
            considered >= config.GAZE_MIN_SAMPLES
            and ratio >= config.GAZE_MIN_FACING_RATIO
        )

        now = time.monotonic()
        cooling = would and (now - _last_grant_t) < config.GAZE_COOLDOWN_S
        verdict = (
            "WOULD_WAKE" if would and not cooling
            else "cooldown" if cooling
            else "skip"
        )
        # The trail is what makes a refusal diagnosable: a single number cannot
        # distinguish "never looked" from "looked, but the detector blinked".
        trail = ",".join(
            "-" if yaw == float("inf") else f"{yaw:.0f}"
            for _, yaw, _, _ in samples[-8:]
        )
        logger.info(
            "[gaze] speech: yaw=%s face=%spx edge=%s facing=%.0f%%/%.0f%% of %d "
            "trail=[%s] -> %s%s",
            yaw_txt, px_txt, edge_txt, ratio * 100.0,
            config.GAZE_MIN_FACING_RATIO * 100.0, considered, trail, verdict,
            " (shadow)" if config.GAZE_WAKE_SHADOW else "",
        )
        if not would or cooling or config.GAZE_WAKE_SHADOW:
            return False

        import hal.app_state as state

        voice = getattr(state, "voice_service", None)
        if voice is None or not hasattr(voice, "grant_wakeword_focus"):
            return False
        granted = bool(voice.grant_wakeword_focus("gaze"))
        if granted:
            _last_grant_t = now
        return granted
    except Exception as e:
        logger.debug("[gaze] speech-start check skipped: %s", e)
        return False


def _maybe_repoint(now: float) -> None:
    """Turn toward the remembered bearing when nobody has been visible.

    Deliberately conservative: this is the one thing in the watcher that MOVES
    the lamp, and a background thread that turns the head unasked is alarming if
    it happens often. Guarded by a long absence, a long cooldown, a confidence
    floor, and by never touching the body while anything else owns it.
    """
    global _last_repoint_t

    if not (config.GAZE_REPOINT_ENABLED and config.GAZE_WAKE_ENABLED):
        return
    if (now - _last_face_t) < config.GAZE_REPOINT_AFTER_S:
        return
    if (now - _last_repoint_t) < config.GAZE_REPOINT_COOLDOWN_S:
        return

    import hal.app_state as state

    from hal.drivers.tracking import aim, user_bearing

    svc = getattr(state, "animation_service", None)
    if svc is None or getattr(svc, "_tracking_active", False):
        return
    if getattr(svc, "_music_playing", False):
        return  # the groove owns the body

    est = user_bearing.read_estimate()
    if est is None or est.confidence < config.GAZE_REPOINT_MIN_CONFIDENCE:
        return

    try:
        current = svc.get_positions()
        target, step = aim._bearing_step_target(svc, est, current)
        if target is None:
            _last_repoint_t = now  # already there; do not re-check every sample
            return
        duration = aim.min_move_duration(
            state.safety_policy, target, current, aim.MOVE_DURATION_S
        )
        svc.move_and_hold(target, duration=duration)
        _last_repoint_t = now
        logger.info(
            "[gaze] nobody visible for %.0fs — turning %+.1f deg to the "
            "remembered bearing %+.1f (conf=%.2f)",
            now - _last_face_t, step, est.bearing_deg, est.confidence,
        )
    except Exception as e:
        logger.debug("[gaze] repoint skipped: %s", e)


def _loop() -> None:
    interval = 1.0 / max(0.5, config.GAZE_SAMPLE_FPS)
    # Let the camera and detector finish warming before the first sample.
    if _stop.wait(10.0):
        return

    import hal.app_state as state

    from hal.drivers.tracking import aim

    cap = getattr(state, "camera_capture", None)
    if cap is None:
        logger.info("[gaze] no camera on this device — watcher stopping")
        return

    # Hold the consumer for the WHOLE watch, not per sample. Acquiring and
    # releasing around each grab lets the device fall back to reduced capture in
    # between, which is exactly when the next sample needs a frame — the same
    # trap the aim loop hit and solved the same way. The cost is honest: this
    # pins the camera at full capture while the watcher runs, which is the real
    # price of the feature and the reason it ships off by default.
    with aim._camera_consumer(cap):
        while not _stop.is_set():
            try:
                _sample_once()
                _maybe_repoint(time.monotonic())
            except Exception as e:  # a background watcher must never take HAL down
                logger.debug("[gaze] sample skipped: %s", e)
            if _stop.wait(interval):
                return


def start() -> None:
    """Begin watching. Idempotent, and a no-op unless the feature is armed."""
    global _thread
    if not config.GAZE_WAKE_ENABLED:
        return
    # With no wake word there is no gate to open — every utterance already
    # dispatches — so the watcher would burn CPU to decide nothing.
    if not config.WAKEWORD_ENABLED:
        logger.info("[gaze] not starting: wake word disabled, nothing to gate")
        return
    if _thread is not None and _thread.is_alive():
        return
    global _last_face_t, _last_repoint_t
    _last_face_t = _last_repoint_t = time.monotonic()
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="gaze-watcher")
    _thread.start()
    logger.info(
        "[gaze] watching at %.1f fps: yaw<=%.0fdeg and face>=%dpx for %.0f%% "
        "of the last %.1fs%s",
        config.GAZE_SAMPLE_FPS, config.GAZE_MAX_YAW_DEG,
        config.GAZE_MIN_FACE_PX, config.GAZE_MIN_FACING_RATIO * 100.0,
        config.GAZE_WINDOW_S,
        " (SHADOW — logging only, gate not opened)" if config.GAZE_WAKE_SHADOW else "",
    )


def stop() -> None:
    _stop.set()


def reset_for_test() -> None:
    """Clear buffered samples and the cooldown between tests."""
    global _last_grant_t, _last_face_t, _last_repoint_t
    with _samples_lock:
        _samples.clear()
    _last_grant_t = 0.0
    _last_face_t = 0.0
    _last_repoint_t = 0.0
