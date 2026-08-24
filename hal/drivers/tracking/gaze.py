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
# Vertical offset of the last face seen, as a fraction of frame height: negative
# means it sat above the centre. None when nothing measurable was in frame.
_last_dy_frac: Optional[float] = None
# Whether that offset came from a real face or from the coarse person fallback.
_last_dy_from_face: bool = False
_last_pitch_t: float = 0.0
# When a repoint moved the head, and what _last_face_t was at that moment.
# Non-zero means a verdict is owed to the bearing — see _verify_repoint.
_repoint_pending_t: float = 0.0
_repoint_face_t_before: float = 0.0
# Consecutive corrections driven by the fallback rather than by a face.
_blind_pitch_steps: int = 0

# (monotonic timestamp, vertical offset as a fraction of frame height, whether
# it came from a real face). Bounded by GAZE_PITCH_WINDOW_S, and cleared
# whenever the camera moves — see record_dy.
_dy_samples: Deque[Tuple[float, float, bool]] = deque()
_dy_lock = threading.Lock()

# Newest sampled frame and the box measured in it, kept ONLY so a correction can
# show what it acted on. One downscaled frame, replaced each sample.
_last_frame: Any = None
_last_box: Any = None

# Must begin with "sensing_" to be servable by the monitor snapshot route.
SNAPSHOT_CATEGORY: str = "sensing_gaze"


def _save_snapshot(label: str) -> Optional[str]:
    """Write the frame this correction was computed from, annotated. Never raises.

    Mirrors bearing_sampler._save_snapshot so both debug views read identically:
    green box on the detection, red line at frame centre.
    """
    if not getattr(config, "GAZE_SNAPSHOT_ENABLED", True) or _last_frame is None:
        return None
    try:
        import os

        from hal.drivers.tracking.look_debug import encode_annotated

        # both_axes: this loop servos on dy, so the horizontal pair is the
        # one that shows the error being corrected. The aim's view stays
        # vertical-only — it can only move yaw.
        jpg = encode_annotated(_last_frame, _last_box, label, both_axes=True)
        if jpg is None:
            return None
        root = getattr(config, "SNAPSHOT_PERSIST_DIR", "/var/lib/hal/snapshots")
        directory = os.path.join(root, SNAPSHOT_CATEGORY)
        os.makedirs(directory, exist_ok=True)
        # Timestamped, so lexical order is time order and pruning is a slice.
        name = time.strftime("%Y%m%d-%H%M%S") + ".jpg"
        path = os.path.join(directory, name)
        with open(path, "wb") as f:
            f.write(jpg)
        keep = int(getattr(config, "GAZE_SNAPSHOT_KEEP", 40) or 0)
        if keep > 0:
            stale = sorted(
                (f for f in os.listdir(directory) if f.endswith(".jpg")),
                reverse=True,
            )[keep:]
            for old in stale:
                try:
                    os.unlink(os.path.join(directory, old))
                except OSError:
                    pass
        return path
    except Exception as e:
        logger.debug("[gaze] snapshot skipped: %s", e)
        return None


def record_dy(dy: Optional[float], from_face: bool,
              now: Optional[float] = None) -> None:
    """Fold one vertical-offset measurement into the pitch window.

    Unmeasurable frames are dropped rather than recorded as zero: "no face this
    frame" is not "the face is centred", and feeding it in would drag the median
    toward the dead zone and stall the correction.
    """
    if dy is None:
        return
    t = time.monotonic() if now is None else now
    with _dy_lock:
        _dy_samples.append((t, float(dy), bool(from_face)))
        cutoff = t - max(1.0, config.GAZE_PITCH_WINDOW_S)
        while _dy_samples and _dy_samples[0][0] < cutoff:
            _dy_samples.popleft()


def _dy_estimate(now: Optional[float] = None):
    """``(median dy, from_face, n, span_s)`` over the window, or None.

    Median, not mean: idle's roll disturbance is periodic and roughly
    symmetric, but a face leaving frame produces one-sided outliers, and a mean
    would follow them.

    Faces outrank the torso fallback outright rather than being averaged with
    it — the fallback reports a fixed -0.5 whatever the real offset is, so
    blending the two would corrupt a real measurement with a constant.
    """
    t = time.monotonic() if now is None else now
    with _dy_lock:
        rows = [r for r in _dy_samples if r[0] >= t - config.GAZE_PITCH_WINDOW_S]
    if not rows:
        return None
    face_rows = [r for r in rows if r[2]]
    use = face_rows if face_rows else rows
    if len(use) < config.GAZE_PITCH_MIN_SAMPLES:
        return None
    span = use[-1][0] - use[0][0]
    # A full period, not a biased arc of one: half a roll cycle's worth of
    # samples has the disturbance's mean in it, not zero.
    if span < config.GAZE_PITCH_WINDOW_S * 0.8:
        return None
    vals = sorted(v for _, v, _ in use)
    mid = len(vals) // 2
    med = vals[mid] if len(vals) % 2 else 0.5 * (vals[mid - 1] + vals[mid])
    return med, bool(face_rows), len(use), span


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
    # The vertical window too: every offset in it was measured from the pose the
    # camera has just left, so a correction computed from them would be aimed at
    # where the face used to sit. Clearing also spaces corrections apart — the
    # loop cannot act again until a fresh window has refilled, which is the real
    # gap between steps now, not GAZE_PITCH_COOLDOWN_S.
    with _dy_lock:
        _dy_samples.clear()


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
    return _ratio_between(samples, t - window, t)


def _ratio_between(samples: List[Tuple[float, float, float, float]],
                   lo: float, hi: float) -> Tuple[float, int]:
    """``(fraction facing, samples examined)`` over ``[lo, hi]``."""
    measured = [
        s for s in samples
        if lo <= s[0] <= hi and s[1] != float("inf") and s[2] >= config.GAZE_MIN_FACE_PX
    ]
    if not measured:
        return 0.0, 0
    facing = sum(1 for _, yaw, px, edge in measured if facing_lamp(yaw, px, edge))
    return facing / float(len(measured)), len(measured)


def facing_before(now: Optional[float] = None) -> Tuple[float, int]:
    """The same ratio over the window IMMEDIATELY BEFORE the decision window.

    This is the baseline the whole design rests on and never had. The module
    docstring says the signal is "the TRANSITION from looking away to looking
    here" — but facing_ratio only ever measured the present, so a user who sits
    facing the lamp's direction all day passed on every utterance. Presence
    became the signal, which is precisely what the design says must not happen.
    Device-measured 2026-08-24 in shadow: nine of nine accepted gestures had
    flat trails like [13,12,12,11,12,11,13,21] — already facing, no turn.

    The samples were always there. GAZE_BUFFER_S retains more history than
    GAZE_WINDOW_S reads, and the older half was simply never consulted.
    """
    t = time.monotonic() if now is None else now
    window = max(0.0, config.GAZE_WINDOW_S)
    return _ratio_between(snapshot(), t - 2.0 * window, t - window)


def _headroom_from_person(frame: Any, detector: Any) -> Optional[float]:
    """A vertical offset to correct by when no face could be measured.

    Returns a negative fraction (meaning "tilt up") when a person is visible
    with their head cut off by the top of the frame, else None. Deliberately
    coarse: this only has to get a head back INTO frame, at which point the
    face path takes over with a real measurement.
    """
    if frame is None or detector is None:
        return None
    try:
        box = detector.detect(frame, "person", strict=False,
                              min_conf=config.LOOK_AIM_MIN_CONFIDENCE)
    except TypeError:
        try:
            box = detector.detect(frame, "person", strict=False)
        except Exception:
            return None
    except Exception:
        return None
    if box is None:
        return None
    _, y, _, h = box
    frame_h = float(frame.shape[0]) or 1.0
    if float(h) / frame_h < config.LOOK_AIM_MIN_PERSON_HEIGHT_FRAC:
        return None  # too far away to be the person at this desk
    # Touching the top edge means the body continues past it — the head is
    # outside the frame, above.
    if float(y) > 2.0:
        return None
    # A torso filling the frame with no face above it does mean the camera is
    # pointing too low — heads sit above bodies, and this lamp sits below head
    # height on a desk. What made that inference dangerous was not the
    # inference: it was that it stays true no matter how far the neck has
    # already travelled, so acting on it repeatedly is a loop rather than a
    # correction (device-observed: wrist_pitch -89.7 -> -34.6 in eight steps,
    # still climbing). The bound lives in the caller, as a budget of blind
    # steps, which is where a "keep going until you can see" search belongs.
    return -0.5


_last_anchor: Optional[float] = None


def _anchor_idle_from_pose(svc: Any, pose: Any) -> None:
    """Rest the idle loop on a remembered pose known to have seen a face."""
    global _last_anchor
    if not config.GAZE_IDLE_ANCHOR or not isinstance(pose, dict):
        return
    setter = getattr(svc, "set_idle_anchor", None)
    if setter is None:
        return
    anchor = {
        j: float(v) for j, v in pose.items()
        if j in ("base_pitch.pos", "wrist_pitch.pos")
        and isinstance(v, (int, float))
    }
    # The remembered wrist_pitch is a stale vertical aim, and re-resting on it
    # undoes every correction the pitch loop has made. Device-observed after
    # the two stopped firing in the same second: pitch lifted the camera to
    # -31.5, then thirty-four seconds later this anchored idle back on -46.1,
    # from where the face is clipped again. The bearing memory is worth
    # believing about which way to FACE, which is what the move itself uses;
    # about how high to look, a correction made seconds ago from a face on
    # screen beats a number recorded minutes ago. So once pitch has spoken,
    # leave the wrist where it put it and anchor the rest.
    if _last_pitch_t > 0.0:
        anchor.pop("wrist_pitch.pos", None)
    if not anchor:
        return
    pitch = anchor.get("wrist_pitch.pos")
    if _last_anchor is not None and pitch is not None and abs(pitch - _last_anchor) < 1.0:
        return
    setter(anchor)
    # Only when this anchor actually carried a wrist angle — otherwise the
    # pitch loop's memory of where it last put the wrist still stands.
    if pitch is not None:
        _last_anchor = pitch
    logger.info(
        "[gaze] idle now rests at %s",
        " ".join(f"{k.split('.')[0]}={v:+.1f}" for k, v in sorted(anchor.items())),
    )


def _anchor_idle_here(svc: Any, pitch: Optional[float] = None) -> None:
    """Tell the idle loop to breathe around the pose we can see the user from."""
    global _last_anchor
    if not config.GAZE_IDLE_ANCHOR:
        return
    setter = getattr(svc, "set_idle_anchor", None)
    if setter is None:
        return
    # Anchor the whole pitch posture, not one joint. Camera direction depends on
    # base_pitch and wrist_pitch TOGETHER — the arm folds at the base, so that
    # joint both rotates and translates the camera, and the same wrist angle
    # points somewhere different for every base angle. Device-measured: with
    # base_pitch near 0 no wrist angle framed the user (the search reached +14
    # and still missed), while base_pitch -30 with wrist_pitch -78 framed them
    # perfectly. Anchoring one joint therefore preserves a posture that was
    # never the reason the framing worked.
    anchor: Dict[str, float] = {}
    try:
        pose = svc.get_positions()
        for joint in ("base_pitch.pos", "wrist_pitch.pos"):
            if joint in pose:
                anchor[joint] = float(pose[joint])
    except (TypeError, ValueError, AttributeError):
        return
    if pitch is not None:
        anchor["wrist_pitch.pos"] = float(pitch)
    if not anchor:
        return
    pitch = anchor.get("wrist_pitch.pos", 0.0)
    # Only when it has actually moved: re-sending the same anchor every frame
    # would log noise and churn the dict for nothing.
    if _last_anchor is not None and abs(pitch - _last_anchor) < 1.0:
        return
    setter(anchor)
    _last_anchor = pitch
    logger.info(
        "[gaze] idle now breathes around %s",
        " ".join(f"{k.split('.')[0]}={v:+.1f}" for k, v in sorted(anchor.items())),
    )


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

    global _last_dy_frac, _last_dy_from_face, _last_frame, _last_box
    if face is None:
        # No face — but that is exactly the state where the camera is most
        # likely pointing too low, and correcting it needs SOME vertical
        # reference. A person box gives one: it says nothing about which way
        # the head faces, so it can never feed the gate, but a torso whose top
        # is cut off by the frame edge says the head is above the frame.
        #
        # Without this the correction dead-ends. It takes several bounded steps
        # to undo a large offset, and the first step can push a barely-visible
        # face out of view entirely — after which nothing is measurable and the
        # camera stays wrong forever. Device-observed: one correction fired
        # (-67.1 -> -59.1), then every later sample read `-`.
        _last_dy_frac = _headroom_from_person(frame_or_small, detector)
        _last_dy_from_face = False
        record_dy(_last_dy_frac, False)
        _last_frame, _last_box = frame_or_small, None
        record_sample(None, 0.0, 0.0)
        return None
    (fx, fy, fw, fh), landmarks = face
    frame_w = float(small.shape[1]) or 1.0
    frame_h = float(small.shape[0]) or 1.0
    # Negative when the face sits above the frame centre — the case that means
    # the camera is pointing too low to see who is talking.
    _last_dy_frac = ((fy + fh / 2.0) - frame_h / 2.0) / frame_h
    _last_dy_from_face = True
    record_dy(_last_dy_frac, True)
    _last_frame, _last_box = small, (fx, fy, fw, fh)

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
    # too low at, not a face turned away. Record it as unmeasured — the vertical
    # offset above still stands, so _maybe_pitch gets its correction from the
    # very frame the yaw had to be thrown away in.
    yaw = (
        head_yaw_deg(landmarks)
        if landmarks_in_frame(landmarks, frame_w, frame_h)
        else None
    )
    record_sample(yaw, float(fh), edge)
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
        measured_enough = considered >= config.GAZE_MIN_SAMPLES
        facing_now = ratio >= config.GAZE_MIN_FACING_RATIO

        # Did they TURN, or were they already pointing this way? See
        # facing_before. An unmeasurable baseline is not evidence against a
        # turn, so it does not veto — but it is reported, because "we could not
        # tell" and "they turned" must not read alike in the log.
        before_ratio, before_n = facing_before()
        baseline_known = before_n >= config.GAZE_MIN_SAMPLES
        turned = (
            not config.GAZE_REQUIRE_TRANSITION
            or not baseline_known
            or before_ratio <= config.GAZE_TRANSITION_MAX_BEFORE
        )

        would = measured_enough and facing_now and turned

        now = time.monotonic()
        cooling = would and (now - _last_grant_t) < config.GAZE_COOLDOWN_S
        # "blind" is not "skip". Nothing measurable in the window means the lamp
        # could not see, which is a different failure from a user who looked
        # away — they are fixed in different places, and reading alike in the
        # log is how the feature's real state stayed hidden (F17).
        verdict = (
            "WOULD_WAKE" if would and not cooling
            else "cooldown" if cooling
            else "blind" if not measured_enough
            else "no-turn" if facing_now and not turned
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
            "was=%s trail=[%s] -> %s%s",
            yaw_txt, px_txt, edge_txt, ratio * 100.0,
            config.GAZE_MIN_FACING_RATIO * 100.0, considered,
            f"{before_ratio * 100:.0f}%/{before_n}" if baseline_known else "?",
            trail, verdict,
            " (shadow)" if config.GAZE_WAKE_SHADOW else "",
        )
        if not would or cooling or config.GAZE_WAKE_SHADOW:
            return False

        import hal.app_state as state

        voice = getattr(state, "voice_service", None)
        if voice is None or not hasattr(voice, "grant_wakeword_focus"):
            return False
        # A shorter floor than the wake phrase or the button get. Those are
        # deliberate acts; this is an inference from where a head was pointing,
        # and it opens a window that every later turn EXTENDS (voice_service
        # refreshes on each authorised turn). A wrong inference therefore does
        # not cost one turn, it costs a conversation — so the opener that is
        # least sure of itself claims the least floor.
        try:
            granted = bool(
                voice.grant_wakeword_focus("gaze", timeout_s=config.GAZE_WAKE_FOCUS_S)
            )
        except TypeError:
            # Voice service predates the shorter-window argument. Degrade to the
            # full allowance rather than lose the gate entirely — the same
            # fallback _detect_subject uses for a detector without `min_conf`.
            # Silently returning False here would disable the feature on a
            # version skew and look exactly like "nobody ever turns to the lamp".
            logger.debug("[gaze] voice service has no timeout_s — full window")
            granted = bool(voice.grant_wakeword_focus("gaze"))
        if granted:
            _last_grant_t = now
        return granted
    except Exception as e:
        logger.debug("[gaze] speech-start check skipped: %s", e)
        return False


def _maybe_pitch(now: float) -> None:
    """Raise or lower the camera so a face sits inside the frame, not above it.

    This is the neck the aim never had — see GAZE_PITCH_ENABLED for which joint
    turned out to be it and how that was measured. Absolute `move_and_hold`,
    never a relative nudge: the sign risk that kept pitch out of the aim lives
    in the relative path, and an absolute target has no sign to get wrong.

    Corrects toward the frame centre and stops there. It does not track a face
    continuously — the point is only to stop the head pointing at the desk.
    """
    global _last_pitch_t, _blind_pitch_steps

    if not (config.GAZE_PITCH_ENABLED and config.GAZE_WAKE_ENABLED):
        return
    est = _dy_estimate(now)
    if est is None:
        return  # window not filled yet — see GAZE_PITCH_WINDOW_S
    dy, from_face, n_dy, span_dy = est
    if abs(dy) <= config.GAZE_PITCH_DEAD_ZONE_FRAC:
        return
    if (now - _last_pitch_t) < config.GAZE_PITCH_COOLDOWN_S:
        return
    # The fallback is a guess, not a measurement — it says "the head is up
    # there somewhere", never how far. Let it walk the camera a few steps and
    # then stop until a real face confirms the direction was right. Otherwise a
    # guess that stays true forever moves the neck forever.
    if not from_face:
        if _blind_pitch_steps >= config.GAZE_PITCH_MAX_BLIND_STEPS:
            return

    import hal.app_state as state

    from hal.drivers.tracking import constants as C

    svc = getattr(state, "animation_service", None)
    if svc is None or getattr(svc, "_tracking_active", False):
        return
    if getattr(svc, "_music_playing", False):
        return

    try:
        current = svc.get_positions()
        cur = float(current.get("wrist_pitch.pos", 0.0))
    except Exception as e:
        logger.debug("[gaze] pitch skipped, no pose: %s", e)
        return

    # A face ABOVE centre (dy < 0) needs the camera tilted UP, and up is the
    # DECREASING direction on this joint — the opposite of what this loop
    # assumed. Device-measured on lamp-0c89, paired A/B/A with the servo bus
    # held quiet, 18 face samples per position interleaved over three cycles so
    # a subject who shifts cancels out:
    #
    #   wrist_pitch -75 -> median dy +0.009 | -90 -> +0.113  (moved -15, dy +0.103)
    #   wrist_pitch -68 -> median dy -0.078 | -98 -> +0.108  (moved -30, dy +0.185)
    #
    # Lowering the joint moves the face DOWN the frame, i.e. the camera swung
    # UP. With the old sign every correction enlarged the error it was
    # measuring, so the loop ratcheted one way until it hit the stop —
    # device-observed -72.6 -> -54.7 -> -42.9 -> -28.2 while each look still
    # reported the face above centre, which is what a wrong sign looks like
    # from the outside.
    step = dy * config.GAZE_PITCH_DEG_PER_FRAME
    step = max(-config.GAZE_PITCH_MAX_STEP_DEG,
               min(config.GAZE_PITCH_MAX_STEP_DEG, step))
    target = max(C.WRIST_PITCH_MIN, min(C.WRIST_PITCH_MAX, cur + step))
    if abs(target - cur) < 0.5:
        return

    try:
        from hal.drivers.tracking import aim

        duration = aim.min_move_duration(
            state.safety_policy, {"wrist_pitch.pos": target}, current,
            aim.MOVE_DURATION_S,
        )
        # Own the body for the move. Without this the correction competes with
        # whatever else is writing the arm, and something always is: idle plays
        # absolutely on every joint and never stops, and an emotion dispatched
        # mid-move re-poses the head entirely (`aim.servo_ownership` exists for
        # exactly that). Both were listed as candidate causes for this loop
        # walking rather than converging.
        #
        # The guard above already declined to start while someone else owns the
        # arm, so this only ever claims a free body; ownership is restored, not
        # cleared, so a tracking session that began meanwhile is not released.
        with aim.servo_ownership():
            svc.move_and_hold({"wrist_pitch.pos": target}, duration=duration)
        # Anchor on EVERY correction, including the guessed ones. Leaving the
        # anchor behind during a search means idle keeps pulling back to where
        # the search started and erases it step by step — device-observed the
        # head reaching -32.4 and being dragged to -41.5 before the next look.
        # A search whose progress is undone between steps is not a search. The
        # blind budget already bounds how far a guess may take this.
        _anchor_idle_here(svc, target)
        discard_samples()
        _last_pitch_t = now
        _blind_pitch_steps = 0 if from_face else _blind_pitch_steps + 1
        logger.info(
            "[gaze] %s %.0f%% %s centre (median of %d over %.1fs) — "
            "wrist_pitch %+.1f -> %+.1f%s",
            "face" if from_face else "head (from torso)",
            abs(dy) * 100.0, "above" if dy < 0 else "below", n_dy, span_dy,
            cur, target,
            "" if from_face
            else f" [blind {_blind_pitch_steps}/{config.GAZE_PITCH_MAX_BLIND_STEPS}]",
        )
        # Last, deliberately: this is debug output, and it must not be able to
        # skip the bookkeeping above. A snapshot that failed before
        # discard_samples() would leave the window uncleared and let the loop
        # correct again from offsets measured at the pose it has just left.
        _save_snapshot(
            f"{'face' if from_face else 'torso'} dy={dy * 100:+.0f}% "
            f"(median of {n_dy} over {span_dy:.1f}s) "
            f"wrist_pitch {cur:+.1f} -> {target:+.1f}"
        )
    except Exception as e:
        logger.debug("[gaze] pitch move skipped: %s", e)


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
    # Not while the framing is being corrected. These two both move the head
    # and both re-anchor idle, so together they fought: device-observed, pitch
    # lifted the camera to a face it could see (-72.8 -> -61.9) and thirteen
    # seconds later repoint anchored idle back on the REMEMBERED pose, wrist
    # -46.7, dropping the aim to where the face was clipped again — over and
    # over, wrist_pitch restarting from -67.8, then -72.8, then -43.6.
    #
    # Precedence goes to pitch because the two answer different questions and
    # only one of them has current evidence: pitch is correcting toward a face
    # visible RIGHT NOW, while repoint is a guess about where a face was last
    # seen, which is worth acting on only when nothing is visible at all.
    if (now - _last_pitch_t) < config.GAZE_REPOINT_AFTER_S:
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
        # Owned for the same reason as the pitch correction, and more so: this
        # one restores a whole posture across four joints, so an emotion landing
        # in the middle leaves the head in a blend of two poses rather than
        # either of them.
        with aim.servo_ownership():
            svc.move_and_hold(target, duration=duration)
        # Make idle rest here too. Turning to the remembered pose is undone
        # within one idle cycle otherwise — idle is absolute and loops, so it
        # walks the camera back to the pose the recording was made at. The
        # anchor comes from the REMEMBERED pose rather than from wherever the
        # watcher happens to be standing: that pose is the one a face was
        # actually seen from, which is the only reason to rest anywhere.
        _anchor_idle_from_pose(svc, getattr(est, "pose", None))
        discard_samples()
        _last_repoint_t = now
        # Owe the estimate a verdict. Deliberately not measured here: a
        # synchronous detect would need the detector lock and a settle, inside
        # a watcher whose whole design is never to make a live look wait. The
        # sampler is already looking at the new view several times a second, so
        # the honest answer arrives by itself a few seconds from now.
        global _repoint_pending_t, _repoint_face_t_before
        _repoint_pending_t = now
        _repoint_face_t_before = _last_face_t
        logger.info(
            "[gaze] nobody visible for %.0fs — turning %+.1f deg to the "
            "remembered bearing %+.1f (conf=%.2f)",
            now - _last_face_t, step, est.bearing_deg, est.confidence,
        )
    except Exception as e:
        logger.debug("[gaze] repoint skipped: %s", e)


def _verify_repoint(now: float) -> None:
    """Tell the bearing whether turning to it actually found the user.

    A hit is `_last_face_t` having advanced past the moment of the turn: that
    clock only moves for a face big enough to measure AND inside
    GAZE_WELL_FRAMED_EDGE, which is exactly the question "did turning here find
    them", not the weaker "was anything visible".

    Scored once per repoint. A miss is not conclusive on its own — the user may
    simply be out of the room — which is why user_bearing requires several
    clustered misses before dropping anything.
    """
    global _repoint_pending_t

    if _repoint_pending_t <= 0.0:
        return
    if (now - _repoint_pending_t) < config.GAZE_REPOINT_VERIFY_S:
        return
    hit = _last_face_t > _repoint_face_t_before
    _repoint_pending_t = 0.0
    try:
        from hal.drivers.tracking import user_bearing

        dropped = user_bearing.record_prediction(hit)
    except Exception as e:
        logger.debug("[gaze] repoint scoring skipped: %s", e)
        return
    logger.info(
        "[gaze] repoint %s — %s%s",
        "found the user" if hit else "found nobody",
        "bearing confirmed" if hit else "counted against the bearing",
        " (estimate dropped)" if dropped else "",
    )


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
                # Framing first: a face inside the frame is what everything
                # else is measured from, and turning to a bearing that still
                # points at the desk finds nobody however right the bearing is.
                _maybe_pitch(now)
                _maybe_repoint(now)
                _verify_repoint(now)
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
    global _last_dy_frac, _last_pitch_t, _blind_pitch_steps, _last_dy_from_face
    with _samples_lock:
        _samples.clear()
    with _dy_lock:
        _dy_samples.clear()
    _last_grant_t = 0.0
    _last_face_t = 0.0
    _last_repoint_t = 0.0
    globals()["_repoint_pending_t"] = 0.0
    globals()["_repoint_face_t_before"] = 0.0
    _last_dy_frac = None
    globals()["_last_frame"] = None
    globals()["_last_box"] = None
    _last_pitch_t = 0.0
    _blind_pitch_steps = 0
    _last_dy_from_face = False
    globals()["_last_anchor"] = None
