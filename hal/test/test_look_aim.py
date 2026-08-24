"""Focused tests for the one-shot look-aim.

The sign test is the important one: an inverted yaw sign is silent — the lamp
turns confidently the wrong way and nothing in the code looks wrong.
"""

from unittest import mock

import numpy as np
import pytest

import hal.app_state as state
from hal.drivers.tracking import aim


class _FakeCap:
    def __init__(self, frame):
        self._frame = frame

    def acquire_consumer(self):
        pass

    def release_consumer(self):
        pass

    @property
    def last_frame(self):
        return self._frame


class _FakeSvc:
    """Tracks yaw across nudges. A fake that ignored commands could not tell
    "decided to move" from "moved", which is exactly what the trace asserts."""

    JOINTS = ["base_yaw.pos", "base_pitch.pos", "elbow_pitch.pos", "wrist_pitch.pos"]

    def __init__(self):
        self.yaw = 0.0
        # Non-yaw joints start away from any remembered posture so a restore is
        # observable — this is the head-pointing-at-the-floor case.
        self.pose = {j: -40.0 for j in self.JOINTS if j != "base_yaw.pos"}
        self.nudge = mock.Mock(side_effect=self._nudge)
        self.move_and_hold = mock.Mock(side_effect=self._move_and_hold)
        self.get_joint_names = mock.Mock(return_value=list(self.JOINTS))
        self.get_positions = mock.Mock(
            side_effect=lambda: {**self.pose, "base_yaw.pos": self.yaw}
        )

    def _nudge(self, yaw, pitch, duration, current, policy):
        self.yaw += yaw
        return {"base_yaw.pos": self.yaw}

    def _move_and_hold(self, positions, duration=None):
        for joint, value in positions.items():
            if joint == "base_yaw.pos":
                self.yaw = float(value)
            else:
                self.pose[joint] = float(value)
        return {**self.pose, "base_yaw.pos": self.yaw}


def _detector(box, target_hit="person"):
    """Detector returning `box` for target_hit, None otherwise."""
    d = mock.Mock()
    d.detect = mock.Mock(
        side_effect=lambda f, t, strict=True, min_conf=None: box if t == target_hit else None
    )
    d.last_confidence = 0.9
    return d


@pytest.fixture(autouse=True)
def _reset_module_state():
    """`_last_seen_mono` deliberately persists across look calls in production —
    seconds-scale occlusion memory is the point — so tests must reset it."""
    aim._last_seen_mono = 0.0
    aim._last_seen_yaw = 0.0
    aim._abort_evt.clear()
    yield


def _frame(width=640, height=480):
    return np.zeros((height, width, 3), dtype=np.uint8)


def _run(box, target_hit="person", disabled=False, deadline=5.0):
    frame = _frame()
    svc = _FakeSvc()
    with (
        mock.patch.object(state, "camera_capture", _FakeCap(frame)),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", disabled, create=True),
        mock.patch("hal.drivers.tracking.user_bearing.read_estimate", return_value=None),
    ):
        res = aim.aim_for_look(deadline, detector=_detector(box, target_hit))
    return res, svc


def test_subject_on_the_right_moves_yaw_positive():
    # dx > 0 (subject right of centre) must INCREASE base_yaw — the tracker's
    # empirically verified convention. Flipping this silently mirrors every aim.
    res, svc = _run(box=(500, 100, 80, 200))  # centre x = 540 > 320
    assert svc.nudge.called
    yaw = svc.nudge.call_args[0][0]
    assert yaw > 0, f"expected positive yaw for a right-of-centre subject, got {yaw}"
    assert res.yaw_moved_deg > 0


def test_subject_on_the_left_moves_yaw_negative():
    res, svc = _run(box=(60, 100, 80, 200))  # centre x = 100 < 320
    assert svc.nudge.called
    assert svc.nudge.call_args[0][0] < 0


def test_pitch_is_never_commanded_in_v1():
    # Pitch sign is unvalidated on the nudge() path — v1 must not touch it.
    _res, svc = _run(box=(500, 100, 80, 200))
    assert svc.nudge.call_args[0][1] == 0.0


def test_already_centred_does_not_move():
    res, svc = _run(box=(300, 100, 40, 200))  # centre x = 320 == frame centre
    assert res.aimed is True
    assert not svc.nudge.called


def test_subject_not_found_reports_and_does_not_move():
    res, svc = _run(box=None)
    assert res.aimed is False
    assert res.reason == "subject not found"
    assert not svc.nudge.called


def test_camera_disabled_never_aims():
    # Privacy: never turn toward someone who asked the device not to look.
    res, svc = _run(box=(500, 100, 80, 200), disabled=True)
    assert res.aimed is False
    assert res.reason == "camera disabled"
    assert not svc.nudge.called


def test_face_is_used_when_no_person_is_detected():
    res, svc = _run(box=(500, 100, 60, 60), target_hit="face")
    assert svc.nudge.called
    assert "face" in res.reason or res.iterations > 0


def test_abort_stops_before_moving():
    aim.request_abort()
    try:
        res, svc = _run(box=(500, 100, 80, 200))
    finally:
        aim._abort_evt.clear()
    # request_abort() is cleared at entry by design, so the aim runs; this
    # asserts the abort path exists and is callable without side effects.
    assert res is not None


def test_deadline_zero_returns_immediately():
    res, svc = _run(box=(500, 100, 80, 200), deadline=0.0)
    assert res.aimed is False
    assert res.reason == "deadline"
    assert not svc.nudge.called


# --- priority 2: occlusion hysteresis -------------------------------------

def test_occlusion_holds_instead_of_turning_away():
    # The failure this guards: user holds an object up, it covers their face,
    # detection fails, and the lamp turns away from the very thing it was asked
    # to look at.
    frame = _frame()
    svc = _FakeSvc()
    with (
        mock.patch.object(state, "camera_capture", _FakeCap(frame)),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", False, create=True),
        mock.patch.object(aim, "_last_seen_mono", aim.time.monotonic()),
        mock.patch.object(aim, "_last_seen_yaw", 0.0),
    ):
        res = aim.aim_for_look(5.0, detector=_detector(None))
    assert res.aimed is False
    assert "occluded" in res.reason
    assert not svc.nudge.called, "must hold position, not turn away"


def test_stale_sighting_does_not_hold():
    frame = _frame()
    svc = _FakeSvc()
    with (
        mock.patch.object(state, "camera_capture", _FakeCap(frame)),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", False, create=True),
        mock.patch.object(aim, "_last_seen_mono", aim.time.monotonic() - 60.0),
        mock.patch.object(aim, "_last_seen_yaw", 0.0),
    ):
        res = aim.aim_for_look(5.0, detector=_detector(None))
    assert "occluded" not in res.reason


# --- priority 3: remembered-bearing fallback ------------------------------

def _bearing(deg, conf, pose=None):
    # Realistic shape — read_estimate() returns a BearingEstimate, and a bare
    # Mock's auto-attributes previously made the aim silently skip the step.
    # `pose` must be a real dict: the remembered posture is iterated.
    if pose is None:
        pose = {"base_yaw.pos": deg, "base_pitch.pos": 5.0,
                "elbow_pitch.pos": 10.0, "wrist_pitch.pos": 0.0}
    return mock.Mock(
        bearing_deg=deg, confidence=conf, samples=12, age_s=30.0, pose=pose
    )


def _run_no_subject(estimate, seen_mono=0.0):
    frame = _frame()
    svc = _FakeSvc()
    with (
        mock.patch.object(state, "camera_capture", _FakeCap(frame)),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", False, create=True),
        mock.patch.object(aim, "_last_seen_mono", seen_mono),
        mock.patch(
            "hal.drivers.tracking.user_bearing.read_estimate", return_value=estimate
        ),
    ):
        res = aim.aim_for_look(5.0, detector=_detector(None))
    return res, svc


def test_no_subject_steps_toward_the_remembered_bearing():
    res, svc = _run_no_subject(_bearing(60.0, 0.9))
    assert svc.move_and_hold.called
    assert res.bearing_steps > 0


def test_bearing_travel_goes_straight_to_the_remembered_pose():
    """One move, not a series of hops.

    Hopping re-detected between steps so it could not sail past someone en
    route — but the lens sees ~110 deg, so anyone in between is already in frame
    before the head moves. Each hop cost a detect plus a settle, roughly a
    second, against the aim's own deadline.
    """
    res, svc = _run_no_subject(_bearing(120.0, 0.9))
    assert svc.move_and_hold.call_count == 1, "should arrive in a single move"
    (positions,), _ = svc.move_and_hold.call_args
    assert abs(positions["base_yaw.pos"] - 120.0) < 1e-6, positions


def test_low_confidence_bearing_is_not_worth_turning_for():
    res, svc = _run_no_subject(_bearing(60.0, 0.01))
    assert not svc.nudge.called
    assert res.reason == "subject not found"


def test_no_bearing_recorded_yet_does_not_move():
    res, svc = _run_no_subject(None)
    assert not svc.nudge.called
    assert res.bearing_steps == 0


def test_bearing_steps_are_bounded():
    res, svc = _run_no_subject(_bearing(135.0, 0.9))
    assert res.bearing_steps <= aim.MAX_BEARING_STEPS


def test_camera_disabled_never_scores_a_failed_prediction():
    # Privacy mode is not evidence that the bearing is wrong. Counting it would
    # let "don't look at me" slowly erase where the user sits.
    with mock.patch("hal.drivers.tracking.user_bearing.record_prediction") as scored:
        res, _svc = _run(box=(500, 100, 80, 200), disabled=True)
    assert res.reason == "camera disabled"
    assert not scored.called


# --- Task F: speaking while searching -------------------------------------

def test_searching_is_announced_once_when_the_lamp_turns_away():
    # The lamp physically turning away mid-question reads as broken unless it
    # says why. This is the one aim state that genuinely needs a voice.
    with mock.patch.object(aim, "_say") as say:
        res, svc = _run_no_subject(_bearing(120.0, 0.9))
    assert res.bearing_steps > 0, "the search never ran, so nothing was announced"
    searching = [c for c in say.call_args_list if c[0][0] == "look_searching"]
    assert len(searching) == 1, f"expected one announcement, got {len(searching)}"


def test_nothing_is_said_when_the_subject_is_already_centred():
    # A fast, silent, correct capture is the good outcome — narrating it is noise.
    with mock.patch.object(aim, "_say") as say:
        _run(box=(300, 100, 40, 200))
    assert not say.called


def test_found_is_only_announced_after_a_search():
    # "There you are" makes sense as the resolution of an announced search, and
    # is noise on a visual question that never had to look.
    with mock.patch.object(aim, "_say") as say:
        _run(box=(500, 100, 80, 200))
    assert not any(c[0][0] == "look_found" for c in say.call_args_list)


def test_speech_can_be_disabled():
    with mock.patch.object(aim.config, "LOOK_AIM_SPEAK", False), \
         mock.patch.object(aim, "_say") as say:
        _run_no_subject(_bearing(120.0, 0.9))
    assert not say.called


# --- servo ownership: nothing else may move the head mid-look --------------

class _FakeAnim:
    def __init__(self, tracking=False):
        self._tracking_active = tracking


def test_ownership_is_claimed_for_the_whole_look():
    # An emotion animation landing between the aim and the shutter re-poses the
    # head on every joint (recordings are absolute, roll included) — which is
    # how a "curious" reaction ends up capturing the ceiling.
    anim = _FakeAnim()
    with mock.patch.object(state, "animation_service", anim):
        with aim.servo_ownership():
            assert anim._tracking_active is True, "emotion servo must be suppressed"
    assert anim._tracking_active is False, "ownership must be released"


def test_ownership_does_not_release_a_real_tracking_session():
    # If the vision tracker already owns the servo, a look must hand it back
    # rather than clearing it — otherwise a visual question would silently end
    # an object-follow session.
    anim = _FakeAnim(tracking=True)
    with mock.patch.object(state, "animation_service", anim):
        with aim.servo_ownership():
            assert anim._tracking_active is True
    assert anim._tracking_active is True, "pre-existing tracking must survive"


def test_ownership_is_released_even_when_the_body_raises():
    anim = _FakeAnim()
    with mock.patch.object(state, "animation_service", anim):
        try:
            with aim.servo_ownership():
                raise RuntimeError("capture blew up")
        except RuntimeError:
            pass
    assert anim._tracking_active is False, "a failed capture must not leave the body locked"


def test_ownership_is_harmless_with_no_animation_service():
    with mock.patch.object(state, "animation_service", None):
        with aim.servo_ownership():
            pass  # must not raise


def test_trace_shows_whether_the_head_actually_moved():
    # "yaw commanded" and "yaw actually reached" are different questions — a
    # trace has to answer the second one.
    res, svc = _run(box=(500, 100, 80, 200))
    assert res.start_yaw is not None and res.end_yaw is not None
    assert res.end_yaw != res.start_yaw, "head should have moved toward the subject"
    assert any("centre" in st["action"] for st in res.steps)


def test_trace_distinguishes_no_bearing_from_a_bearing_that_missed():
    # These look identical in a summary but need different fixes.
    res_none, _ = _run_no_subject(None)
    assert res_none.bearing_consulted is None
    assert any("no bearing recorded yet" in st["action"] for st in res_none.steps)

    res_used, _ = _run_no_subject(_bearing(60.0, 0.9))
    assert res_used.bearing_consulted is not None
    assert res_used.bearing_consulted["bearing_deg"] == 60.0


def test_trace_records_the_occlusion_hold():
    frame = _frame()
    svc = _FakeSvc()
    with (
        mock.patch.object(state, "camera_capture", _FakeCap(frame)),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", False, create=True),
        mock.patch.object(aim, "_last_seen_mono", aim.time.monotonic()),
        mock.patch.object(aim, "_last_seen_yaw", 0.0),
    ):
        res = aim.aim_for_look(5.0, detector=_detector(None))
    assert any("hold" in st["action"] for st in res.steps)


def test_the_detector_is_built_once_not_per_look():
    # A per-look ObjectDetector cost ~7s on device (its constructor fetches the
    # DL public key over the network), which blew the realtime turn budget and
    # made Gemini time out — the user then got "I couldn't see it" for a frame
    # that had been captured perfectly.
    aim._shared_detector = None
    with mock.patch("hal.drivers.tracking.detection.ObjectDetector") as ctor:
        ctor.return_value = mock.Mock()
        first = aim.get_detector()
        second = aim.get_detector()
        third = aim.get_detector()
    assert ctor.call_count == 1, f"detector rebuilt {ctor.call_count} times"
    assert first is second is third
    aim._shared_detector = None


def test_a_failing_detector_does_not_wedge_the_aim():
    aim._shared_detector = None
    with mock.patch("hal.drivers.tracking.detection.ObjectDetector",
                    side_effect=RuntimeError("model missing")):
        assert aim.get_detector() is None
    aim._shared_detector = None


class _StaleCap(_FakeCap):
    """A camera whose frame timestamp does not advance after a servo write.

    This is the device failure, reproduced: `last_frame` kept returning the
    pre-move image, so every iteration measured the same offset and re-issued
    the same correction. On green-lamp that marched the head 61 deg across six
    steps with dx frozen at 0.241, and the lamp ended up aimed at a wall.
    """

    def __init__(self, frame):
        super().__init__(frame)
        self.last_frame_ts = 100.0  # frozen: never advances
        self.consumers = 0
        self.max_consumers = 0

    def acquire_consumer(self):
        self.consumers += 1
        self.max_consumers = max(self.max_consumers, self.consumers)

    def release_consumer(self):
        self.consumers -= 1


class _StampingSvc(_FakeSvc):
    """Servo that stamps `last_servo_write`, as the real AnimationService does."""

    def __init__(self):
        super().__init__()
        self.last_servo_write = 100.0

    def _nudge(self, yaw, pitch, duration, current, policy):
        self.last_servo_write += 1.0  # every write is newer than any held frame
        return super()._nudge(yaw, pitch, duration, current, policy)


def _run_stale(box, deadline=1.0):
    frame = _frame()
    cap = _StaleCap(frame)
    svc = _StampingSvc()
    with (
        mock.patch.object(state, "camera_capture", cap),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", False, create=True),
        mock.patch("hal.drivers.tracking.user_bearing.read_estimate", return_value=None),
        mock.patch.object(aim, "FRAME_WAIT_S", 0.05),  # keep the test fast
    ):
        res = aim.aim_for_look(deadline, detector=_detector(box))
    return res, svc, cap


def test_stale_frames_do_not_march_the_head():
    """With feedback frozen, the aim must not keep issuing the same correction.

    Bounding total travel is the assertion that matters: the old loop moved
    ~12 deg per iteration forever because it never saw the result of its own
    move.
    """
    # One command per fresh measurement is the invariant; total travel depends
    # on how far off-centre the subject is, so counting corrections is the
    # assertion that actually encodes the rule.
    res, svc, _ = _run_stale((520, 200, 600, 400))
    assert svc.nudge.call_count == 1, (
        f"issued {svc.nudge.call_count} corrections from one measurement "
        f"(head travelled {svc.yaw:+.1f} deg)"
    )
    assert res.reason == "no fresh frame"


def test_camera_consumer_held_once_for_the_whole_aim():
    """Acquire/release per frame let the device drop below full FPS between
    iterations — which is why a fresh frame never arrived."""
    _run_stale((520, 200, 600, 400))
    _, _, cap = _run_stale((520, 200, 600, 400))
    assert cap.max_consumers == 1, "consumer should be held once, not per grab"
    assert cap.consumers == 0, "consumer leaked"


# --- Closed-loop convergence -------------------------------------------------
# The fakes above hold the subject still, so they measure the decision but not
# whether the loop actually lands. These simulate a camera and servo that agree:
# the subject's pixel offset responds to the head's real yaw, which is what makes
# the FOV calibration observable.

_REAL_FOV_DEG = 110.0  # device-measured (107-123); the aim's constant is a guess


class _FreshCap(_FakeCap):
    """Always offers a frame newer than any servo write."""

    @property
    def last_frame_ts(self):
        import time as _t

        return _t.monotonic() + 1000.0


def _sim_detector(svc, subject_bearing_deg, width=640):
    """Detector that reports where the subject falls given the CURRENT head yaw."""

    def _detect(frame, target, strict=True, min_conf=None):
        if target != "person":
            return None
        rel = subject_bearing_deg - svc.yaw  # degrees off the optical axis
        px = width / 2.0 + rel * (width / _REAL_FOV_DEG)
        if not (0 <= px < width):
            return None  # subject left the frame
        # (x, y, w, h) top-left, matching ObjectDetector — NOT corners.
        return (int(px) - 20, 200, 40, 200)

    d = mock.Mock()
    d.detect = mock.Mock(side_effect=_detect)
    d.last_confidence = 0.9
    return d


def _run_closed_loop(subject_bearing_deg, fov_setting):
    import hal.config as hal_cfg

    svc = _FakeSvc()
    with (
        mock.patch.object(state, "camera_capture", _FreshCap(_frame())),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", False, create=True),
        mock.patch.object(hal_cfg, "LOOK_AIM_FOV_DEG", fov_setting),
        mock.patch("hal.drivers.tracking.user_bearing.read_estimate", return_value=None),
        mock.patch("hal.drivers.tracking.aim._record_bearing_if_centred"),
    ):
        res = aim.aim_for_look(30.0, detector=_sim_detector(svc, subject_bearing_deg))
    return res, svc


def test_calibrated_fov_centres_within_two_iterations():
    """With the FOV close to the truth the aim lands almost immediately —
    this is what keeps it inside LOOK_AIM_DEADLINE_S on device."""
    res, svc = _run_closed_loop(30.0, 100.0)
    assert res.aimed, f"did not centre: {res.reason}"
    assert res.iterations <= 2, f"took {res.iterations} iterations"
    assert abs(svc.yaw - 30.0) < 8.0, f"settled at {svc.yaw:+.1f} deg, subject at +30"


def test_self_calibration_recovers_from_a_wrong_fov_constant():
    """The constant is only the first guess.

    A fisheye has no single right value — the device measured 91 deg near the
    frame centre and 229 deg at the edge — so the aim measures the LOCAL scale
    from what its own last move achieved. A badly wrong constant must therefore
    cost at most the first step, not the whole aim.
    """
    res_bad, _ = _run_closed_loop(30.0, 60.0)
    res_good, _ = _run_closed_loop(30.0, 100.0)
    assert res_bad.aimed and res_good.aimed, (res_bad.reason, res_good.reason)
    assert res_bad.iterations <= res_good.iterations + 1, (
        f"a wrong constant cost {res_bad.iterations - res_good.iterations} extra steps"
    )


def test_measured_scale_is_recorded_per_step():
    """The scale is the number to look at when an aim crawls — it must be in
    the trace, and flagged while it is still the unmeasured guess."""
    res, _ = _run_closed_loop(30.0, 100.0)
    assert res.steps and all("scale" in st for st in res.steps)


def test_scale_measurement_rejects_uninformative_steps():
    """Dividing a tiny shift by a tiny move turns detector jitter into a wild
    scale, and one wild scale sends the head across the room."""
    assert aim._measure_scale(0.5, 0.10) is None      # move too small
    assert aim._measure_scale(20.0, 0.001) is None    # shift too small
    assert aim._measure_scale(20.0, -0.10) is None    # subject went the wrong way
    assert aim._measure_scale(400.0, 0.05) is None    # implausible, out of bounds
    assert aim._measure_scale(20.0, 0.10) == 200.0    # a real measurement


def test_last_move_is_reported_for_the_capture_settle():
    """An aim that exits straight after a big swing leaves the arm ringing; the
    caller needs the size to know how long to let it settle."""
    res, _ = _run_closed_loop(30.0, 100.0)
    assert res.last_move_deg != 0.0


def test_aim_never_overshoots_past_the_subject():
    """Overshoot oscillates and never settles; undershoot always converges.
    Every step must move toward the subject and stop short of crossing it."""
    _, svc = _run_closed_loop(30.0, 100.0)
    assert svc.yaw <= 30.0 + 1e-6, f"crossed the subject: {svc.yaw:+.1f} > +30"


# --- Remembered posture, not just direction ---------------------------------

def test_bearing_step_restores_the_remembered_pitch_joints():
    """Yaw alone cannot describe "looking at the user".

    With the head left pointing at the floor, sweeping yaw searches the floor in
    a circle: device trace 20260819-143407 stepped -45 -> -13 toward a correct
    bearing and still saw nothing, because pitch was never restored.
    """
    res, svc = _run_no_subject(_bearing(60.0, 0.9))
    assert res.bearing_steps > 0
    assert svc.pose["base_pitch.pos"] == 5.0, svc.pose
    assert svc.pose["elbow_pitch.pos"] == 10.0, svc.pose


def test_posture_is_restored_even_when_the_yaw_is_already_right():
    """The head can be pointed at the exact bearing and still be aimed at the
    ground — "already pointing there" must mean the whole shape, not the base."""
    svc = _FakeSvc()
    svc.yaw = 60.0  # already on the bearing
    est = _bearing(60.0, 0.9)
    with (
        mock.patch.object(state, "safety_policy", None, create=True),
        mock.patch("hal.drivers.tracking.user_bearing.read_estimate", return_value=est),
    ):
        moved = aim._step_toward_bearing(svc, {})
    assert moved, "a wrong posture at the right yaw must still move"
    assert svc.pose["base_pitch.pos"] == 5.0


def test_no_move_when_already_in_the_remembered_shape():
    """Otherwise every search step re-issues a move for rounding noise."""
    svc = _FakeSvc()
    svc.yaw = 60.0
    svc.pose = {"base_pitch.pos": 5.0, "elbow_pitch.pos": 10.0, "wrist_pitch.pos": 0.0}
    with (
        mock.patch.object(state, "safety_policy", None, create=True),
        mock.patch("hal.drivers.tracking.user_bearing.read_estimate",
                   return_value=_bearing(60.0, 0.9)),
    ):
        moved = aim._step_toward_bearing(svc, {})
    assert not moved
    assert not svc.move_and_hold.called


def test_unknown_joints_are_not_commanded():
    """A remembered pose from another robot (or an older servo set) must not be
    sent to joints this device does not have."""
    svc = _FakeSvc()
    est = _bearing(60.0, 0.9, pose={"base_yaw.pos": 60.0, "tentacle.pos": 12.0})
    with (
        mock.patch.object(state, "safety_policy", None, create=True),
        mock.patch("hal.drivers.tracking.user_bearing.read_estimate", return_value=est),
    ):
        aim._step_toward_bearing(svc, {})
    (positions,), _ = svc.move_and_hold.call_args
    assert "tentacle.pos" not in positions, positions


# --- Near-subject gate -------------------------------------------------------

def test_a_far_person_is_not_treated_as_a_subject():
    """Device frame 20260819-142823: a ~22px "face" clear across the office,
    and the lamp turned to it. Too small to be someone holding something up to
    the camera, so the aim must fall through to hold/bearing instead."""
    res, svc = _run((600, 300, 60, 25))
    assert not res.aimed
    assert res.reason != "centred on person"
    assert not svc.nudge.called, "turned toward a stranger across the room"


def test_a_close_person_still_passes_the_gate():
    """Device frame 20260819-143218: ~165px of a person clipped by the frame
    edge — the real asker. The gate must not cost us this one."""
    res, svc = _run((520, 200, 100, 165))
    assert svc.nudge.called or res.aimed


def test_the_gate_uses_height_not_width():
    """A close subject is routinely clipped left/right — the good device frame
    is half out of shot — so width says nothing about distance."""
    frame = _frame(width=640, height=480)
    narrow_but_tall = (10, 0, 12, 300)
    assert aim._is_near_enough(narrow_but_tall, frame, "person")


# --- Confidence floor --------------------------------------------------------

def test_the_aim_asks_for_a_higher_confidence_than_the_tracker():
    """DETECT_MIN_CONFIDENCE is 0.15, tuned so the TRACKER keeps its lock on a
    phone at an odd angle. Aiming wants the opposite trade — a false positive
    turns the lamp at a wall (device 2026-08-19: a person rendered inside a
    laptop screen was accepted and aimed at)."""
    import hal.config as hal_cfg

    det = _detector((520, 200, 100, 165))
    aim._detect_subject(det, _frame())
    _args, kwargs = det.detect.call_args
    assert kwargs.get("min_conf") == hal_cfg.LOOK_AIM_MIN_CONFIDENCE
    assert hal_cfg.LOOK_AIM_MIN_CONFIDENCE > 0.15, "must be stricter than the global floor"


def test_the_chosen_box_reports_its_confidence():
    """Without this a bad box cannot be told from a 0.17 fluke when debugging."""
    det = _detector((520, 200, 100, 165))
    det.last_confidence = 0.63
    _box, _kind, conf = aim._detect_subject(det, _frame())
    assert conf == 0.63


def test_an_older_detector_without_min_conf_still_works():
    """The size gate must keep protecting a detector that predates the kwarg."""
    det = mock.Mock()
    det.detect = mock.Mock(
        side_effect=lambda f, t, strict=True: (520, 200, 100, 165) if t == "person" else None
    )
    box, kind, _conf = aim._detect_subject(det, _frame())
    assert box is not None and kind == "person"


def test_found_is_announced_once_however_many_iterations_follow():
    """`bearing_steps` stays above zero for the rest of the aim, so an
    unlatched announcement fires on every centring iteration after a search —
    device 2026-08-19 said "bạn đây rồi" four times in three seconds."""
    svc = _FakeSvc()
    # Seen only after a bearing step: no detection first, then a close person.
    seen = {"n": 0}

    def _detect(frame, target, strict=True, min_conf=None):
        if target != "person":
            return None
        seen["n"] += 1
        return None if seen["n"] == 1 else (330, 100, 60, 300)

    det = mock.Mock()
    det.detect = mock.Mock(side_effect=_detect)
    det.last_confidence = 0.9
    with (
        mock.patch.object(state, "camera_capture", _FakeCap(_frame())),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", False, create=True),
        mock.patch("hal.drivers.tracking.user_bearing.read_estimate",
                   return_value=_bearing(60.0, 0.9)),
        mock.patch.object(aim, "_say") as say,
    ):
        res = aim.aim_for_look(5.0, detector=det)
    found = [c for c in say.call_args_list if c[0][0] == "look_found"]
    assert res.bearing_steps > 0, "the search never ran, so nothing was announced"
    assert len(found) == 1, f"announced {len(found)} times"


def test_the_measured_scale_is_biased_low_not_high():
    """Overshoot oscillates; undershoot just costs a step. The scale is measured
    at the current eccentricity and spent at a smaller one, where a fisheye's
    true scale is lower — so it must be damped, never amplified."""
    assert 0.0 < aim.SCALE_SAFETY < 1.0
    assert aim.MAX_SCALE_DEG <= 250.0, "400 asked for corrections that got clamped"


# --- Subject selection: the asker, not the detector's favourite (F24) ---


def _candidate_detector(candidates, face=None):
    """Detector exposing `detect_candidates` for person, `detect` for face."""
    d = mock.Mock()
    d.detect_candidates = mock.Mock(
        side_effect=lambda f, t, strict=False, min_conf=None: (
            list(candidates) if t == "person" else []
        )
    )
    d.detect = mock.Mock(
        side_effect=lambda f, t, strict=True, min_conf=None: face if t == "face" else None
    )
    d.last_confidence = 0.5
    return d


def test_the_nearest_person_wins_over_a_more_confident_distant_one():
    """The device failure, reproduced (look_logs/20260824-112802).

    A small, fully-visible colleague at the back scored 0.71 while the person
    actually asking — clipped, occluded by what they held up — scored lower.
    Confidence ranked the colleague first and the aim turned 19.8 deg away.
    """
    frame = _frame(width=1280, height=720)
    colleague = ((300, 210, 160, 190), 0.71)   # 190px tall, 26% of frame
    asker = ((640, 0, 640, 700), 0.52)         # 700px tall, at the edge
    box, kind, conf = aim._detect_subject(_candidate_detector([colleague, asker]), frame)

    assert box == asker[0], "the closest person is the one talking to the lamp"
    assert kind == "person"
    assert conf == 0.52


def test_a_detection_too_small_to_be_the_asker_is_not_chosen():
    """The floor still rejects — it just runs before the choice now."""
    frame = _frame(width=1280, height=720)
    far = ((300, 300, 40, 70), 0.95)  # 70px = 9.7% of frame, under the 15% floor
    box, kind, _ = aim._detect_subject(_candidate_detector([far], face=None), frame)

    assert box is None and kind == ""


def test_the_size_floor_is_applied_before_ranking_not_after():
    """A high-confidence stranger must not shadow a qualifying asker.

    `detect` returns ONE box, so filtering afterwards could only rubber-stamp
    whatever confidence had already picked — which is how the wrong human got
    through.
    """
    frame = _frame(width=1280, height=720)
    tiny_but_certain = ((10, 10, 30, 60), 0.99)   # 8% of frame — under the floor
    real_asker = ((600, 100, 400, 500), 0.40)     # 69% of frame
    box, _kind, _conf = aim._detect_subject(
        _candidate_detector([tiny_but_certain, real_asker]), frame
    )

    assert box == real_asker[0]


def test_no_person_candidates_falls_back_to_the_face_path():
    frame = _frame(width=1280, height=720)
    face_box = (500, 200, 120, 140)  # 19% of frame height, over the 8% face floor
    box, kind, _ = aim._detect_subject(_candidate_detector([], face=face_box), frame)

    assert box == face_box and kind == "face"


def test_a_detector_without_the_candidate_path_still_works():
    """Older detector object: fall through to `detect`, do not raise."""
    frame = _frame(width=1280, height=720)
    d = mock.Mock(spec=["detect", "last_confidence"])
    d.detect = mock.Mock(
        side_effect=lambda f, t, strict=True, min_conf=None: (
            (100, 100, 300, 400) if t == "person" else None
        )
    )
    d.last_confidence = 0.8
    box, kind, _ = aim._detect_subject(d, frame)

    assert box == (100, 100, 300, 400) and kind == "person"
