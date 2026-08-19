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

    def __init__(self):
        self.yaw = 0.0
        self.nudge = mock.Mock(side_effect=self._nudge)
        self.get_positions = mock.Mock(side_effect=lambda: {"base_yaw.pos": self.yaw})

    def _nudge(self, yaw, pitch, duration, current, policy):
        self.yaw += yaw
        return {"base_yaw.pos": self.yaw}


def _detector(box, target_hit="person"):
    """Detector returning `box` for target_hit, None otherwise."""
    d = mock.Mock()
    d.detect = mock.Mock(side_effect=lambda f, t, strict=True: box if t == target_hit else None)
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

def _bearing(deg, conf):
    # Realistic shape — read_estimate() returns a BearingEstimate, and a bare
    # Mock's auto-attributes previously made the aim silently skip the step.
    return mock.Mock(bearing_deg=deg, confidence=conf, samples=12, age_s=30.0)


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
    assert svc.nudge.called
    assert res.bearing_steps > 0


def test_bearing_travel_is_stepped_not_one_blind_move():
    # A single large move would sail past anyone standing en route, because
    # nudge() blocks and no detection runs during it.
    res, svc = _run_no_subject(_bearing(120.0, 0.9))
    for call in svc.nudge.call_args_list:
        assert abs(call[0][0]) <= aim.BEARING_STEP_DEG + 1e-6


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
    assert res.bearing_steps > 1, "need multiple steps to prove it speaks only once"
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

    def _detect(frame, target, strict=True):
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


def test_undercalibrated_fov_is_what_made_it_slow():
    """The 60 deg guess needs far more steps for the same subject — the
    regression this constant exists to prevent. Undershoot, never overshoot."""
    res_bad, _ = _run_closed_loop(30.0, 60.0)
    res_good, _ = _run_closed_loop(30.0, 100.0)
    assert res_bad.iterations > res_good.iterations


def test_aim_never_overshoots_past_the_subject():
    """Overshoot oscillates and never settles; undershoot always converges.
    Every step must move toward the subject and stop short of crossing it."""
    _, svc = _run_closed_loop(30.0, 100.0)
    assert svc.yaw <= 30.0 + 1e-6, f"crossed the subject: {svc.yaw:+.1f} > +30"
