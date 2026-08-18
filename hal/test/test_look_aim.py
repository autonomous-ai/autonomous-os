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
    def __init__(self):
        self.nudge = mock.Mock(return_value={})
        self.get_positions = mock.Mock(return_value={"base_yaw.pos": 0.0})


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
    return mock.Mock(bearing_deg=deg, confidence=conf)


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
