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
    and speech normally triggers a read BACKWARDS through it. This mirrors what
    the mic already does with its own pre-roll: it records into a lookback and
    recovers the ~640 ms before the trigger, or it would lose the start of every
    sentence. If no usable face evidence exists at all, the watcher first
    restores the remembered pose and the completed same utterance gets one
    constrained recovery check.

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
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

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
# A VAD-confirmed utterance found no recent usable face. The watcher consumes
# this request and restores the remembered pose without blocking the mic loop;
# the completed utterance gets one final gaze check after the camera settles.
_speech_repoint_requested = threading.Event()
_speech_repoint_requested_t: float = 0.0


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


def landmarks_in_frame(landmarks: Sequence[float], width: float,
                       height: float) -> bool:
    """Whether every landmark the yaw is built from was actually SEEN.

    YuNet reports landmarks for a face clipped by a frame edge as freely as for
    one wholly inside it, and the ones outside come back with coordinates off
    the frame — device-measured on this lamp, a user sitting plainly in front
    of it: box ``[264, -1, 162, 92]`` with both eyes at ``y = -3.0`` and
    ``y = -1.3``. Those two numbers are an extrapolation, not an observation.

    Fed to head_yaw_deg they push the nose ratio past 1, where the clamp turns
    "not measurable" into exactly 90.0 — a number indistinguishable from a real
    profile, and one facing_ratio then counts as a valid vote AGAINST. That is
    how a user looking straight at the lamp produced `trail=[90,90,90,90]`.

    So the rule is the same one the size floor already states: a measurement
    that cannot be made must not vote either way. Only the first three points —
    the two eyes and the nose — matter, because they are the only ones the yaw
    reads; a mouth corner below the chin line being clipped says nothing about
    the angle.
    """
    try:
        pts = [float(v) for v in landmarks[:6]]
    except (TypeError, ValueError):
        return False
    if len(pts) < 6:
        return False
    for x, y in zip(pts[0::2], pts[1::2]):
        if not (math.isfinite(x) and math.isfinite(y)):
            return False
        if x < 0.0 or y < 0.0 or x > width or y > height:
            return False
    return True


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


def discard_samples() -> None:
    """Forget everything measured before the camera moved.

    Samples describe a head's angle as seen from ONE camera pose. Once the neck
    moves, the older ones describe a pose that no longer exists, and they vote
    anyway: device-observed `trail=[40,48,41,44,-,44,1,2]`, where the 40s are
    the badly-framed past and the 1 and 2 are the user now plainly facing the
    lamp — scored together, they refused a gesture that had just succeeded.

    This is the same rule the aim already applies to frames captured before a
    servo write, for the same reason.
    """
    with _samples_lock:
        _samples.clear()


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


def following_a_face(svc: Any) -> bool:
    """Whether the servo writes are a tracking session pursuing the user.

    Tracking writes the arm every frame to keep a person centred, so
    `last_servo_write` is never stale while it runs — which put the whole of
    tracking back behind the settling test, through the back door, after that
    test was written specifically NOT to use `_tracking_active`. Device-
    measured with tracking up: 0.7 samples/s recorded against 4.5/s blocked,
    and a user at yaw 0.9 deg with a 130 px face dead centre was refused for
    having one sample in the window instead of two.

    The reason the original rule rejected `_tracking_active` still holds, and
    is stronger here than for idle: tracking is the lamp FOLLOWING this user's
    face. Refusing to notice they are addressing it, precisely then, is the
    most broken-looking moment available. A pursuit is also a small continuous
    correction rather than a relocation — the same shape as idle breathing, for
    the same reason the yaw survives it.
    """
    return bool(svc is not None and getattr(svc, "_tracking_active", False))


def idle_breathing(svc: Any) -> bool:
    """Whether the only thing writing the servos is the idle loop looping.

    The animation service marks that state itself: `_idle_settled` is set when
    the idle recording reaches its end and starts round again at reduced FPS,
    and cleared the moment anything else takes the arm — an emotion, a tracking
    session, a commanded move, or the interpolation INTO idle, which is a real
    relocation and reads as one here too.

    This is a read of an existing flag rather than a new seam on purpose: the
    service already had to know the difference to keep the lamp from startling
    at its own joints (see is_noisy_motion), and a second, parallel notion of
    "is it moving" would be one more thing to keep in step.
    """
    if svc is None or not getattr(svc, "_idle_settled", False):
        return False
    current = getattr(svc, "_current_recording", None)
    return current is not None and current == getattr(svc, "idle_recording", None)


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


def _sample_once() -> Optional[str]:  # noqa: C901
    """One camera observation folded into the buffer.

    Returns None when a sample was recorded, else the reason it was skipped.
    """
    global _last_face_t

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
    # Skip only while the head is actually MOVING — a yaw measured mid-swing
    # describes the lamp's motion, not the user's intent.
    #
    # Not `_tracking_active`. That flag stays set for the whole of a tracking
    # session, and a session is exactly when the lamp is following the user's
    # face: the state where it is most obviously attending to them, and where
    # refusing to notice they are addressing it reads as broken. Tracking also
    # holds still most of the time — it corrects, then waits — so the flag
    # blocks far more than the moving head it was standing in for.
    #
    # And not every servo write is a move. The idle loop breathes: it writes
    # the arm every frame, forever, so `last_servo_write` is almost never older
    # than FRAME_SETTLE_S and this test alone refused nearly every frame.
    # Device-measured once the loop started counting recorded samples rather
    # than attempts: 0.3 samples/s recorded against 4.9/s blocked — 94% of the
    # evidence thrown away, which no window size or sample floor downstream can
    # recover from. Idle motion is millimetres and slow; the yaw survives it.
    # A settling window exists for a real relocation, so it applies to one.
    last_write = getattr(svc, "last_servo_write", 0.0)
    if isinstance(last_write, (int, float)) and last_write > 0:
        if (time.monotonic() - float(last_write)) < aim.FRAME_SETTLE_S:
            if not (idle_breathing(svc) or following_a_face(svc)):
                return "head still settling from a move"

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
        frame_or_small = small
        detector = aim.get_detector()
        face = detection.detect_face_with_landmarks(small)
    finally:
        aim._detector_lock_use.release()

    if face is None:
        record_sample(None, 0.0, 0.0)
        return None
    (fx, fy, fw, fh), landmarks = face
    frame_w = float(small.shape[1]) or 1.0
    frame_h = float(small.shape[0]) or 1.0
    # Distance of the face centre from the frame centre, 0 at the middle and 1
    # at either edge — how far into the lens distortion this sample sits.
    edge = min(1.0, abs((fx + fw / 2.0) - frame_w / 2.0) / (frame_w / 2.0))
    # Height of the face as DETECTED, i.e. in the downscaled frame the landmarks
    # were measured in — that is the resolution the yaw precision depends on.
    if float(fh) >= config.GAZE_MIN_FACE_PX and edge <= config.GAZE_WELL_FRAMED_EDGE:
        # Big enough to measure AND not about to leave the frame. Both halves
        # matter: background colleagues fail the size floor, and a user drifting
        # to the edge used to keep resetting this clock while sliding out of
        # view, so the lamp never turned to keep them.
        _last_face_t = time.monotonic()
    # A face whose eyes sit outside the frame is a face this camera is pointed
    # too low at, not a face turned away. Record it as unmeasured rather than
    # letting the clamp report it as a profile and vote against facing.
    yaw = (
        head_yaw_deg(landmarks)
        if landmarks_in_frame(landmarks, frame_w, frame_h)
        else None
    )
    record_sample(yaw, float(fh), edge)
    return None


def _check_speech(stage: str, *, request_repoint: bool) -> bool:
    """Decide whether the current utterance was addressed to the lamp.

    The normal start check uses only evidence from before speech. A missing face
    is different from a facing-away verdict: it requests one return to the
    remembered user pose, then the final check may use the evidence gathered
    while that same utterance was being captured.
    """
    global _last_grant_t, _speech_repoint_requested_t
    try:
        if not config.GAZE_WAKE_ENABLED:
            return False
        # A noise-only capture may never reach on_speech_end() because it has
        # no transcript. Starting a fresh VAD-confirmed utterance supersedes
        # that abandoned request rather than letting it leak into this one.
        if request_repoint:
            _speech_repoint_requested_t = 0.0
            _speech_repoint_requested.clear()

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
            "[gaze] %s: yaw=%s face=%spx edge=%s facing=%.0f%%/%.0f%% of %d "
            "trail=[%s] -> %s%s",
            stage, yaw_txt, px_txt, edge_txt, ratio * 100.0,
            config.GAZE_MIN_FACING_RATIO * 100.0, considered, trail, verdict,
            " (shadow)" if config.GAZE_WAKE_SHADOW else "",
        )
        if not would:
            # Do not turn toward somebody who was measured facing away: that is
            # precisely the signal that the nearby speech may be for somebody
            # else. Reacquire only when there was too little usable evidence to
            # make any orientation decision at all.
            if request_repoint and considered < config.GAZE_MIN_SAMPLES:
                _speech_repoint_requested_t = now
                _speech_repoint_requested.set()
                logger.info(
                    "[gaze] %s: no usable face evidence; requesting remembered-pose reacquire",
                    stage,
                )
            return False
        if cooling or config.GAZE_WAKE_SHADOW:
            return False

        import hal.app_state as state

        voice = getattr(state, "voice_service", None)
        if voice is None or not hasattr(voice, "grant_wakeword_focus"):
            return False
        source = "gaze" if stage == "speech-start" else "gaze-reacquired"
        granted = bool(voice.grant_wakeword_focus(source))
        if granted:
            _last_grant_t = now
        return granted
    except Exception as e:
        logger.debug("[gaze] %s check skipped: %s", stage, e)
        return False


def on_speech_start() -> bool:
    """Check the pre-speech gaze window when VAD confirms speech."""
    return _check_speech("speech-start", request_repoint=True)


def on_speech_end() -> bool:
    """Retry one utterance after a voice-triggered pose reacquire.

    This is intentionally conditional on a failed start check with no usable
    face. It is not a general second chance for a person who was observed
    speaking away from the lamp.
    """
    global _speech_repoint_requested_t
    if _speech_repoint_requested_t <= 0.0:
        return False
    _speech_repoint_requested_t = 0.0
    _speech_repoint_requested.clear()
    return _check_speech("speech-end", request_repoint=False)


def _consume_speech_repoint(now: float) -> None:
    """Run a pending VAD request from the watcher, never from the mic thread."""
    if not _speech_repoint_requested.is_set():
        return
    _speech_repoint_requested.clear()
    if not _maybe_repoint(now, force=True):
        logger.info("[gaze] speech-start reacquire unavailable")


def _maybe_repoint(now: float, *, force: bool = False) -> bool:
    """Turn toward the remembered bearing when nobody has been visible.

    Deliberately conservative: this is the one thing in the watcher that MOVES
    the lamp, and a background thread that turns the head unasked is alarming if
    it happens often. Guarded by a long absence, a long cooldown, a confidence
    floor, and by never touching the body while anything else owns it.
    """
    global _last_repoint_t

    if not (config.GAZE_REPOINT_ENABLED and config.GAZE_WAKE_ENABLED):
        return False
    if not force and (now - _last_face_t) < config.GAZE_REPOINT_AFTER_S:
        return False
    # Voice may bypass the long *absence* delay, but never the movement
    # cooldown. VAD intentionally admits some noise so it cannot make the lamp
    # repeatedly turn just because no face is visible.
    if (now - _last_repoint_t) < config.GAZE_REPOINT_COOLDOWN_S:
        return False
    import hal.app_state as state

    from hal.drivers.tracking import aim, user_bearing

    svc = getattr(state, "animation_service", None)
    if svc is None or getattr(svc, "_tracking_active", False):
        return False
    if getattr(svc, "_music_playing", False):
        return False  # the groove owns the body

    est = user_bearing.read_estimate()
    if est is None or est.confidence < config.GAZE_REPOINT_MIN_CONFIDENCE:
        return False

    try:
        current = svc.get_positions()
        target, step = aim._bearing_step_target(svc, est, current)
        if target is None:
            _last_repoint_t = now  # already there; do not re-check every sample
            logger.info(
                "[gaze] %s: already at remembered bearing %+.1f",
                "speech-start reacquire" if force else "repoint",
                est.bearing_deg,
            )
            return True
        duration = aim.min_move_duration(
            state.safety_policy, target, current, aim.MOVE_DURATION_S
        )
        svc.move_and_hold(target, duration=duration)
        discard_samples()
        _last_repoint_t = now
        if force:
            logger.info(
                "[gaze] speech-start reacquire: turning %+.1f deg to remembered "
                "bearing %+.1f (conf=%.2f)",
                step, est.bearing_deg, est.confidence,
            )
        else:
            logger.info(
                "[gaze] nobody visible for %.0fs — turning %+.1f deg to the "
                "remembered bearing %+.1f (conf=%.2f)",
                now - _last_face_t, step, est.bearing_deg, est.confidence,
            )
        return True
    except Exception as e:
        logger.debug("[gaze] repoint skipped: %s", e)
        return False


def _loop() -> None:
    interval = 1.0 / max(0.5, config.GAZE_SAMPLE_FPS)
    counted = 0
    # Blocked turns BY REASON. One total says evidence is being lost; it does
    # not say which gate is losing it, and the two are fixed in different
    # places — this cost a round of guessing at 4.5/s blocked before the cause
    # was pinned on the settling test rather than on the detector lock.
    blocked: Dict[str, int] = {}
    counted_from = 0.0
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
                skipped = _sample_once()
                now = time.monotonic()
                # Report the rate ACHIEVED, and achieved means RECORDED. Counting
                # iterations instead counts the turns that recorded nothing —
                # a frame refused for settling or for a busy detector leaves the
                # buffer exactly as it was — and the two numbers are not close:
                # this loop reported 5.7/s on the device while the buffer held
                # samples older than the 1.5 s window, i.e. under 1/s of real
                # evidence, which is what starved GAZE_MIN_SAMPLES and refused
                # users who were plainly facing the lamp. Every window-size and
                # min-samples decision downstream reads this figure, so it has
                # to mean what it says.
                if skipped is None:
                    counted += 1
                else:
                    blocked[skipped] = blocked.get(skipped, 0) + 1
                if counted_from <= 0.0:
                    counted_from = now
                elif (now - counted_from) >= 60.0:
                    elapsed = now - counted_from
                    why = ", ".join(
                        f"{n / elapsed:.1f}/s {reason}"
                        for reason, n in sorted(
                            blocked.items(), key=lambda kv: -kv[1]
                        )
                    ) or "nothing blocked"
                    logger.info(
                        "[gaze] sampling at %.1f/s (asked %.1f/s); blocked: %s",
                        counted / elapsed, config.GAZE_SAMPLE_FPS, why,
                    )
                    counted, blocked, counted_from = 0, {}, now
                _consume_speech_repoint(now)
                _maybe_repoint(now)
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
    global _speech_repoint_requested_t
    with _samples_lock:
        _samples.clear()
    _last_grant_t = 0.0
    _last_face_t = 0.0
    _last_repoint_t = 0.0
    _speech_repoint_requested_t = 0.0
    _speech_repoint_requested.clear()
