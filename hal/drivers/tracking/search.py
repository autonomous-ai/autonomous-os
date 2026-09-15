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
from typing import Any, Callable, List, Optional

import hal.app_state as state
from hal.drivers.tracking import constants as C
from hal.drivers.tracking.aim import _detect_subject, _grab_frame

logger = logging.getLogger(__name__)

# How far the base turns between stops.
#
# 90, not 45, because the head now looks around at each stop and covers the gap.
# With the ring reaching +/-45 of roll and a ~100 deg lens (LOOK_AIM_FOV_DEG), one bearing
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

# How fast the base may turn WHILE SWEEPING, in STS3215 Goal_Speed units.
#
# The register has to be written to take effect at all: it reads 0 ("no limit")
# on every joint, yet base_yaw does 16 deg/s untouched and 115 deg/s straight
# after writing that same 0 back. Unwritten, a 90 deg search turn took 4.8s and
# the sweep spent most of its time waiting for the arm.
#
# Set here rather than at startup on purpose. Writing it for every joint when
# the driver boots would change how the whole robot moves — idle, emotions,
# every recorded animation — and none of that asked to be sped up. The sweep
# needs a brisk base; nothing else does.
#
# Linear, measured on device: 400 -> 30 deg/s, 700 -> 52, 1000 -> 71, 1400 -> 92,
# so about 0.062 deg/s per unit. 1200 gives roughly 80, which is quick enough to
# stop the sweep dragging without the whole lamp whipping round beside someone.
SWEEP_YAW_SPEED: int = 1200
MAX_STOPS: int = 3
# How far, as a fraction of the frame, the centring probe will follow the
# nearest candidate between two frames. One correction is capped at
# MAX_STEP_DEG (45) on a lens measured at ~91 deg per frame-width at the centre,
# so the object being chased moves under half the frame per step — and can
# overshoot to the far side of centre. Anything further than this could not be
# the same object. Deliberately loose: the same-side swap that actually happens
# on a two-keyboard desk is caught by the direction test below, not by distance.
STICKY_MAX_JUMP_FRAC: float = 0.55
# Slack on the "toward the centre" test: detector jitter on a box edge is a few
# percent of the frame and must not read as the object retreating.
STICKY_AWAY_TOL_FRAC: float = 0.06

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
# How far the camera tilts from the seed pitch at the top and bottom of the
# look circle. Positive is DOWN (gaze._maybe_pitch's convention).
PITCH_LOOK_DEG: float = 25.0
# The corners go to FULL roll and FULL pitch, not cos(45) of each. That makes
# the ring a rounded square rather than a true circle, and it is deliberate:
# device-checked 2026-09-09, the corners then see as much to the side as the
# left/right looks do and as far down as the bottom look does, so each corner
# adds new ground instead of re-covering the middle. A true ellipse pulled the
# diagonals in to 31.8/17.7 and gave up that reach for a shape nobody watches.
ROLL_DIAG_DEG: float = ROLL_LOOK_DEG
PITCH_DIAG_DEG: float = PITCH_LOOK_DEG

# The looks the head walks at each bearing, as (wrist_roll, pitch offset).
# Positive roll is right, positive pitch is down.
#
# A clock face, not a raster. The base turns ONCE per bearing and the head does
# all the looking from there — where the previous design stepped the base
# through every bearing again for each pitch tier, retracing the whole arc two
# or three times to cover the same ground.
#
# Only the two WRIST joints move. The pitch tiers used to go through
# servo_follow.distribute_pitch, which spreads a tilt across base_pitch,
# elbow_pitch and wrist_pitch against each joint's travel — right for a tracking
# correction, wrong for a sweep. Device-observed 2026-09-09: the upward tier put
# elbow_pitch at +35.8 while base_pitch dropped to +10.6, extending the arm up
# and back far enough to look like it might tip. distribute_pitch allocates
# against per-joint travel; nothing in it knows about the arm's balance. Moving
# the wrist alone keeps the mass where it is and reads as a head looking around
# rather than a body reshaping itself.
#
# Order: centre, then round the ring from the left, through the bottom, to the
# right, then over the top. The first six are the default sweep — a half-moon
# below the horizon, because the things people ask the lamp to find sit on desks.
# It also ends on the right, which is the direction the next bearing lies in, so
# the handover is a 45 deg step rather than a swing back across everything.
LOOK_CIRCLE = (
    (0.0, 0.0),                             # centre
    (-ROLL_LOOK_DEG, 0.0),                  # left
    (-ROLL_DIAG_DEG, +PITCH_DIAG_DEG),      # bottom-left
    (0.0, +PITCH_LOOK_DEG),                 # bottom
    (+ROLL_DIAG_DEG, +PITCH_DIAG_DEG),      # bottom-right
    (+ROLL_LOOK_DEG, 0.0),                  # right
    (+ROLL_DIAG_DEG, -PITCH_DIAG_DEG),      # top-right
    (0.0, -PITCH_LOOK_DEG),                 # top
    (-ROLL_DIAG_DEG, -PITCH_DIAG_DEG),      # top-left
)
HALF_LOOKS: int = 6

# Margin held off the wrist_pitch soft stop. Device-measured 2026-09-09 on
# lamp-ac82: wrist_pitch reached -89.55 going up (stopped by WRIST_PITCH_MIN,
# not by the joint) and -16.61 going down with no stall at all — so the real
# travel is far wider than PITCH_TRAVEL_MIN/MAX in constants.py claims (-33..+32),
# which were measured in a different arm configuration and are not usable here.
#
# Upward is what runs out: resting near -73 there are only ~16 degrees before
# the soft stop, less than PITCH_LOOK_DEG. The circle is therefore clamped per
# sweep against the seed pose rather than assumed symmetric.
WRIST_PITCH_MARGIN: float = 2.0

_abort_evt = threading.Event()


def request_abort() -> None:
    """Stop an in-flight search. Wired to the physical button, whose single
    click means "stop moving and pay attention to me"."""
    _abort_evt.set()


@dataclass
class SearchResult:
    found: bool
    reason: str
    # Renamed from `stops_visited`: this counts LOOKS, and the API rendered it
    # as "after N stop(s)" against MAX_STOPS = 3, so an exhaustive sweep
    # truthfully reported "after 27 stop(s)" out of a possible 3.
    looks_visited: int = 0
    found_at_yaw: Optional[float] = None
    found_at_roll: Optional[float] = None
    # How many BEARINGS the base actually turned through. Reported separately
    # because "3 bearings, 27 looks" is the sentence the caller needs, and
    # neither number on its own can be turned into it.
    bearings_visited: int = 0
    kind: Optional[str] = None
    box: Optional[tuple] = None
    centred: bool = False
    # Absolute path to the annotated JPEG of the winning frame, or None when
    # nothing was found (a picture of an empty wall under a caption saying the
    # search failed is worse than no picture).
    image_path: Optional[str] = None


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
    t0 = time.monotonic()
    deadline = t0 + ARRIVE_TIMEOUT_S
    polls = 0
    last: Optional[dict] = None
    while time.monotonic() < deadline:
        try:
            poll_t = time.monotonic()
            now_pose = svc.get_positions()
            read_s = time.monotonic() - poll_t
            polls += 1
        except Exception:
            return
        if last is not None and all(
            abs(float(now_pose.get(j, 0.0)) - float(last.get(j, 0.0))) < ARRIVE_STILL_DEG
            for j in target
        ):
            logger.info("[search] settled in %.2fs (%d polls, last read %.0fms)",
                        time.monotonic() - t0, polls, read_s * 1000)
            return
        last = now_pose
        time.sleep(0.1)
    logger.info("[search] still moving after %.1fs — shooting anyway", ARRIVE_TIMEOUT_S)


def _look_at(svc: Any, roll: float, wrist_pitch: Optional[float] = None,
             yaw: Optional[float] = None) -> bool:
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
        if wrist_pitch is not None:
            target["wrist_pitch.pos"] = float(wrist_pitch)
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


def _sticky_probe(detector: Any, target: str, first_box: tuple) -> Callable[[Any], Optional[tuple]]:
    """A probe for the centring correction that keeps the INSTANCE the sweep
    found, when the frame holds more than one of the class.

    `detect` answers "which one box" by confidence, and that is the wrong
    question mid-correction: device-observed on lamp-ac82, a desk with a laptop
    keyboard and a black keyboard had the loop chase whichever scored higher on
    each frame — dx -11% -> -43% -> +30% — until the deadline. Confidence says
    how canonical a keyboard looks, not which keyboard the user meant.

    So every probe asks for all candidates and takes the one nearest the box it
    is already centring on, then moves the anchor there. After a correction the
    object has shifted in the frame by roughly the step, and the other instance
    has shifted by the same amount, so nearest-to-previous keeps following the
    right one as long as the two are further apart than one step — which a
    laptop and a keyboard on the same desk are.

    Targets with no candidate list — open-vocab nouns served remotely, and
    person/face with their closest-subject policy — fall back to the single-box
    path, which is what they had before.
    """
    anchor = {"box": tuple(first_box)}

    def _centre(b: tuple) -> tuple:
        return (b[0] + b[2] / 2.0, b[1] + b[3] / 2.0)

    def probe(frame: Any) -> Optional[tuple]:
        cands: list = []
        getter = getattr(detector, "detect_candidates", None)
        if getter is not None and target not in ("person", "face"):
            try:
                cands = list(getter(frame, target) or [])
            except Exception as e:
                logger.debug("[search] detect_candidates failed, using detect: %s", e)
                cands = []
        if cands:
            ax, ay = _centre(anchor["box"])
            box = min((tuple(b) for b, _conf in cands),
                      key=lambda b: (_centre(b)[0] - ax) ** 2 + (_centre(b)[1] - ay) ** 2)
            # Nearest is not the same as near. On one device frame the detector
            # returned ONLY the other keyboard, and nearest-of-one sent the
            # correction 40% across the frame in a single step. One correction
            # moves the object by at most MAX_STEP_DEG over a ~100 deg lens —
            # under half the frame — so anything further has to be a different
            # object. A miss here costs one fresh frame; a jump costs the find.
            bx, by = _centre(box)
            fh, fw = frame.shape[0], frame.shape[1]
            if abs(bx - ax) > STICKY_MAX_JUMP_FRAC * fw or abs(by - ay) > STICKY_MAX_JUMP_FRAC * fh:
                logger.info("[search] probe: nearest %s is %.0f%% away — not the same one, treating as a miss",
                            target, 100.0 * max(abs(bx - ax) / fw, abs(by - ay) / fh))
                return None
            # A swap can be small — device-observed 20%, under any distance
            # cutoff a legitimate step could also produce. The stronger test is
            # direction: every correction moves ITS object toward the centre, so
            # a candidate further from centre than the anchor, on the same side,
            # cannot be the object that was just corrected. Overshoot past the
            # centre is a real outcome and stays allowed.
            for prev, now, size in ((ax, bx, fw), (ay, by, fh)):
                p_off, n_off = prev - size / 2.0, now - size / 2.0
                same_side = (p_off < 0) == (n_off < 0)
                if same_side and abs(n_off) > abs(p_off) + STICKY_AWAY_TOL_FRAC * size:
                    logger.info("[search] probe: nearest %s moved AWAY from centre (%.0f%% -> %.0f%%) — "
                                "another instance, treating as a miss",
                                target, 100.0 * p_off / size, 100.0 * n_off / size)
                    return None
        else:
            box = _detect_target(detector, frame, target)[0]
        if box is not None:
            anchor["box"] = tuple(box)
        return box

    return probe


def _detect_target(detector: Any, frame: Any, target: str):
    """Find `target` in the frame. Returns (box, kind), or (None, None).

    person/face keep `_detect_subject`, whose closest-person choice and face
    fallback exist for "where are YOU" and would be wrong for an object. Any
    other noun goes to the detector's own by-name path — the same YOLOv8n/
    YOLOWorld chain `/servo/track` uses, so a search can name anything a track
    can.
    """
    from hal.drivers.tracking.aim import _detect_subject

    if target in ("person", "face"):
        box, kind, _conf = _detect_subject(detector, frame)
        return box, kind
    box = detector.detect(frame, target)
    return box, (target if box is not None else None)


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
    logger.info("[search] aborted after %d look(s) — back to the starting pose", visited)
    return SearchResult(False, "aborted", visited)


def _persist_hit(frame: Any, box: Any, label: str) -> Optional[str]:
    """Write the frame the sweep stopped on, with the detection drawn on it.

    Into the SAME pool /camera/snapshot?save=true uses — the active runtime's
    media dir, capped and rotated — for two reasons. The agent's image tool
    only reads inside its own allow-list, and os-server's thumbnail path only
    surfaces a file under an approved runtime dir (camera_snapshot.go). A JPEG
    written anywhere else is a file nobody can open.

    Annotated rather than raw: the answer to "did you find my keyboard" is not
    a photograph of a desk, it is a photograph of a desk with a box round the
    keyboard. The drawing helper is the one the look-aim debug frames already
    use, so the box the user sees is the box the detector actually returned —
    but with `centre_lines` off, because the dx pair is the aim's working and
    this image has a person for a reader.

    `frame` and `box` MUST come from the same grab. Drawing a box measured
    before the centring correction onto a frame captured after it puts a green
    rectangle next to the object instead of round it, which is a worse answer
    than no picture at all.

    Never raises — a search that found the thing must not fail over a JPEG.
    """
    try:
        import os

        from hal.drivers.tracking.look_debug import encode_annotated

        jpg = encode_annotated(frame, box, label, centre_lines=False)
        if jpg is None:
            return None
        os.makedirs(state._SNAPSHOT_DIR, exist_ok=True)
        path = os.path.join(state._SNAPSHOT_DIR,
                            f"snap_{int(time.time() * 1000)}.jpg")
        with open(path, "wb") as f:
            f.write(jpg)
        state._snapshot_paths.append(path)
        while len(state._snapshot_paths) > state._SNAPSHOT_MAX:
            oldest = state._snapshot_paths.pop(0)
            try:
                os.remove(oldest)
            except OSError:
                pass
        return path
    except Exception as e:
        logger.warning("[search] could not persist the winning frame: %s", e)
        return None


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


def _say_at_the_midpoint() -> Callable[[int, int], None]:
    """A progress handler that breaks the silence once, halfway through.

    A sweep is up to half a minute of the lamp swinging without a word. One
    phrase before it starts does not cover that, and repeating `look_searching`
    would ask "where are you?" twice, which sounds stuck rather than patient.

    The default for every sweep, not just the look-aim's. Whoever started it —
    the user asking outright, or the aim giving up — is waiting through the same
    silence.
    """
    said = {"done": False}

    def handler(visited: int, total: int) -> None:
        if said["done"] or visited * 2 < total:
            return
        said["done"] = True
        from hal.drivers.tracking.aim import _say

        _say("look_still_searching")

    return handler


def search_for_subject(target: str = "person", detector: Any = None,
                       on_progress: Optional[Callable[[int, int], None]] = None,
                       exhaustive: bool = False) -> SearchResult:
    """Sweep for a subject, stopping at the first one seen.

    Returns rather than raising: a failed search still has to give the caller
    something to say.
    """
    _abort_evt.clear()

    # A search for a THING stops at the first sighting, always. `exhaustive`
    # is a survey mode — "scan the room", "is anyone else here" — that covers
    # every look, counts sightings and goes home, which is the right shape for
    # a headcount and the wrong one for a find: device-observed 2026-09-14, an
    # agent that passed it for "find my doll" got a lamp that saw the doll five
    # times, returned to its seed pose, and reported a find with no picture.
    # Enforced here rather than trusted to the skill text, because the model
    # is the one deciding what to pass.
    if exhaustive and target not in ("person", "face"):
        logger.info("[search] exhaustive ignored for '%s' — an object search "
                    "stops at the first sighting", target)
        exhaustive = False

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

    # Narrating the midpoint is the default; a caller passes its own handler
    # only to do something else, and `on_progress=lambda *_: None` to stay quiet.
    if on_progress is None:
        on_progress = _say_at_the_midpoint()

    with aim.servo_ownership():
        capped = svc.set_joint_speed("base_yaw", SWEEP_YAW_SPEED)
        try:
            return _sweep(svc, cap, detector, target, on_progress, exhaustive)
        finally:
            if capped:
                # Back to the resting value the driver writes at startup — 0, no
                # cap. A cap here would follow the sweep out and throttle idle
                # and every emotion, which move base_yaw in small 30fps steps and
                # need no speed limit. The sweep is the only thing that wants one.
                svc.set_joint_speed(
                    "base_yaw",
                    getattr(svc, "UNWRITTEN_SPEED_EQUIVALENT", 0),
                )


def _look_list(seed_pose: Optional[dict], exhaustive: bool) -> list:
    """Absolute (roll, wrist_pitch) for every look at one bearing.

    Pitch is absolute, measured from the seed pose, so re-entering the ring
    cannot accumulate an offset into a stop — the same discipline the yaw stop
    list follows. Clamped against WRIST_PITCH_MIN/MAX because the headroom
    above the resting pose is smaller than PITCH_LOOK_DEG on a lamp that starts
    with its head low; a clamped look still looks somewhere useful, where a
    commanded overrun just stalls the servo.
    """
    base_wp = None
    if seed_pose:
        try:
            base_wp = float(seed_pose["wrist_pitch.pos"])
        except (KeyError, TypeError, ValueError):
            base_wp = None
    pattern = LOOK_CIRCLE if exhaustive else LOOK_CIRCLE[:HALF_LOOKS]
    lo = C.WRIST_PITCH_MIN + WRIST_PITCH_MARGIN
    hi = C.WRIST_PITCH_MAX - WRIST_PITCH_MARGIN
    out = []
    for roll, dp in pattern:
        wp = None if base_wp is None else max(lo, min(hi, base_wp + dp))
        out.append((roll, wp))
    return out


def _sweep(svc: Any, cap: Any, detector: Any, target: str,
           on_progress: Optional[Callable[[int, int], None]] = None,
           exhaustive: bool = False) -> SearchResult:
    """The sweep itself, with the body already owned.

    `on_progress(visited, total)` is called after every look. It exists so a
    caller can fill the silence — a sweep is half a minute of a lamp moving
    without saying anything — while leaving the decision of WHAT to say, and
    whether to say anything at all, outside this file.
    """
    # Deferred, like every other reach into `aim` here: that module imports from
    # this one, so a top-level import either way is a cycle.
    from hal.drivers.tracking import aim

    stops = _stop_list(_seed_yaw(svc))
    # Captured AFTER seeding, so it is the pose the sweep started from
    # rather than whatever the arm was doing before — that is where a
    # failed search should leave the lamp.
    try:
        seed_pose = {j: float(v) for j, v in svc.get_positions().items()
                     if j.endswith('.pos')}
    except Exception:
        seed_pose = None
    looks = _look_list(seed_pose, exhaustive)
    total_looks = len(stops) * len(looks)
    logger.info("[search] sweeping %d bearings x %d looks (%d total) for '%s': %s",
                len(stops), len(looks), total_looks, target,
                [round(s) for s in stops])

    visited = 0
    bearings = 0
    found: List[SearchResult] = []
    for yaw in stops:
        bearings += 1
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
        for n, (roll, wrist_pitch) in enumerate(looks):
            if _abort_evt.is_set():
                return _abandon(svc, seed_pose, visited)
            # The first look of a stop carries the base turn with it, so the
            # handover from the previous stop is one continuous movement.
            if not _look_at(svc, roll, wrist_pitch,
                            yaw=yaw if n == 0 else None):
                continue

            # Settle before reading: a head still ringing gives a blurred frame
            # and a detector that misses what is actually in view.
            time.sleep(SETTLE_S)
            visited += 1
            if on_progress is not None:
                try:
                    on_progress(visited, total_looks)
                except Exception as e:
                    # A talkative caller must never be able to sink the search.
                    logger.debug("[search] progress callback failed: %s", e)

            _t_grab = time.monotonic()
            frame = _grab_frame(cap)
            _grab_ms = (time.monotonic() - _t_grab) * 1000
            if frame is None:
                continue
            _t_det = time.monotonic()
            box, kind = _detect_target(detector, frame, target)
            logger.info("[search] look %d/%d: grab %.0fms detect %.0fms -> %s",
                        visited, total_looks, _grab_ms,
                        (time.monotonic() - _t_det) * 1000,
                        kind or "nothing")
            if box is not None:
                logger.info(
                    "[search] found %s at yaw %+.0f roll %+.0f after %d look(s)",
                    kind, yaw, roll, visited,
                )
                hit = SearchResult(True, f"found {kind}", visited, yaw, roll,
                                   bearings, kind, box)
                if not exhaustive:
                    # Correct FIRST, straighten AFTER. The object is proven in
                    # view from exactly this pose, so this is where the
                    # correction's first probe is most likely to see it again.
                    # Straightening first was device-observed (lamp-ac82, twice)
                    # to cost the find: the base turned, the head re-levelled,
                    # and the correction's first frames came from a body that
                    # had just moved — `centring: lost the subject after 0
                    # iteration(s)`.
                    #
                    # The straighten preserves the camera's direction (base
                    # takes what the head gives up), so a box centred now stays
                    # centred through it. And without any correction at all the
                    # sweep pointed at `yaw + roll` — the look DIRECTION of the
                    # stop, not the subject — so an object at the frame edge
                    # left the lamp aimed ~50 deg away from it while reporting
                    # a find.
                    centred = aim.centre_on_box(
                        svc, cap, probe=_sticky_probe(detector, target, box),
                    )
                    hit.centred = centred.centred
                    if centred.box is not None:
                        hit.box = centred.box
                    logger.info("[search] centring: %s after %d iteration(s)",
                                centred.reason, centred.iterations)
                    # Straighten from where the correction actually left the
                    # base, not from the stop it started at.
                    try:
                        now_pose = svc.get_positions()
                        now_yaw = float(now_pose.get("base_yaw.pos", yaw))
                        now_roll = float(now_pose.get("wrist_roll.pos", roll))
                    except Exception:
                        now_yaw, now_roll = yaw + centred.yaw_total, roll
                    _straighten_head_onto(svc, now_yaw, now_roll)
                    hit.found_at_yaw = now_yaw
                    # Prefer the CENTRED frame and its box — that pair is what
                    # the lamp is pointing at now. Fall back to the frame that
                    # triggered the hit when the correction never got one (no
                    # fresh frame, an abort, a failed nudge): the sweep did see
                    # the thing, and "found it" with nothing to show is the
                    # answer this whole path exists to stop. Always a matched
                    # (frame, box) pair, never one from each.
                    shot_frame, shot_box = ((centred.frame, centred.box)
                                            if centred.frame is not None
                                            else (frame, box))
                    hit.image_path = _persist_hit(shot_frame, shot_box, kind)
                    return hit
                found.append(hit)

    if exhaustive and found:
        _restore(svc, seed_pose)
        logger.info("[search] full sweep: %d sighting(s) of '%s' across %d looks",
                    len(found), target, visited)
        return SearchResult(True, f"found {target} x{len(found)}", visited,
                            found[0].found_at_yaw, found[0].found_at_roll,
                            bearings, found[0].kind, found[0].box)

    # Nothing found, so nothing to look at — go back to where the sweep began
    # rather than freezing wherever the last look left the head.
    _restore(svc, seed_pose)
    logger.info("[search] no %s found after %d look(s) — back to the starting pose",
                target, visited)
    return SearchResult(False, f"no {target} found", visited,
                        bearings_visited=bearings)
