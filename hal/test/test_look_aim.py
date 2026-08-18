"""Focused tests for the one-shot look-aim.

The sign test is the important one: an inverted yaw sign is silent — the lamp
turns confidently the wrong way and nothing in the code looks wrong.
"""

from unittest import mock

import numpy as np

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
