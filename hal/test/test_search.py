"""Focused tests for the deliberate search sweep.

The behaviours that matter: it looks at the likely place FIRST, its stops
overlap so nobody falls between them, it stays inside the mechanical range,
and it stops the moment it finds someone rather than completing the sweep.
"""

from unittest import mock

import numpy as np
import pytest

import hal.app_state as state
from hal.drivers.tracking import constants as C
from hal.drivers.tracking import search


@pytest.fixture(autouse=True)
def _reset():
    search._abort_evt.clear()
    yield


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
        self.yaw = 0.0
        self.holds = []            # absolute poses restored before the sweep
        self.nudge = mock.Mock(side_effect=self._nudge)
        # A real arm reports every joint. `_bearing_step_target` treats a joint
        # ABSENT from the current pose as already-correct, so a double that
        # returns yaw alone can never restore pitch — and would hide the very
        # thing this file now tests.
        self.get_positions = mock.Mock(side_effect=lambda: {
            "base_yaw.pos": self.yaw,
            "base_pitch.pos": 0.0,
            "elbow_pitch.pos": 0.0,
            "wrist_pitch.pos": 0.0,
        })

    def get_joint_names(self):
        return ["base_yaw.pos", "base_pitch.pos", "elbow_pitch.pos", "wrist_pitch.pos"]

    def move_and_hold(self, target, duration=None):
        self.holds.append(dict(target))
        if "base_yaw.pos" in target:
            self.yaw = float(target["base_yaw.pos"])

    def _nudge(self, y, p, d, cur, pol):
        self.yaw += y
        return {"base_yaw.pos": self.yaw}


def _run(detect_at_stop=None, bearing=None, disabled=False, abort_at_stop=None,
         confidence=0.9, pose=None):
    """detect_at_stop: 1-based stop index at which a subject appears."""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    svc = _FakeSvc()
    calls = {"n": 0}

    def _detect(f, t, strict=True):
        # _detect_subject probes "person" then "face" at each stop, so count
        # only the first probe to get the stop number.
        if t == "person":
            calls["n"] += 1
            if abort_at_stop is not None and calls["n"] >= abort_at_stop:
                search.request_abort()
        if detect_at_stop is not None and calls["n"] >= detect_at_stop:
            return (300, 100, 40, 200) if t == "person" else None
        return None

    det = mock.Mock()
    det.detect = mock.Mock(side_effect=_detect)
    if bearing is None:
        est = None
    else:
        est = mock.Mock(bearing_deg=bearing, confidence=confidence)
        # A real estimate carries a whole posture; the seed restores it before
        # sweeping (see _seed_from_bearing).
        est.pose = {"base_yaw.pos": bearing} if pose is None else pose

    with (
        mock.patch.object(state, "camera_capture", _FakeCap(frame)),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", disabled, create=True),
        mock.patch.object(search.time, "sleep"),
        mock.patch("hal.drivers.tracking.user_bearing.read_estimate", return_value=est),
    ):
        res = search.search_for_subject(detector=det)
    return res, svc


def test_stops_overlap_so_nobody_falls_between_them():
    # Stepping by a full FOV would leave seams; a person straddling two tiles
    # would be missed by both.
    assert search.STEP_DEG < C.CAMERA_FOV_DEG, "step must be smaller than the field of view"


def test_search_starts_at_the_remembered_bearing():
    stops = search._stop_list(60.0)
    assert stops[0] == 60.0, "the likely place must be checked first"


def test_search_expands_outward_from_the_seed():
    stops = search._stop_list(0.0)
    assert stops[1:3] == [search.STEP_DEG, -search.STEP_DEG]


def test_stops_stay_inside_the_mechanical_range():
    for seed in (-135.0, 0.0, 135.0):
        for y in search._stop_list(seed):
            assert C.YAW_MIN <= y <= C.YAW_MAX, f"{y} outside servo limits"


def test_seed_beyond_the_limit_is_clamped():
    assert search._stop_list(999.0)[0] == C.YAW_MAX


def test_stops_on_first_sighting_rather_than_completing_the_sweep():
    res, svc = _run(detect_at_stop=2)
    assert res.found is True
    assert res.stops_visited == 2, "should stop as soon as it sees someone"


def test_reports_failure_after_exhausting_the_sweep():
    res, svc = _run(detect_at_stop=None)
    assert res.found is False
    assert res.reason == "nobody found"
    assert res.stops_visited > 1


def test_camera_disabled_never_sweeps():
    # A search is a lot of conspicuous movement to perform while the user has
    # asked the device not to look.
    res, svc = _run(detect_at_stop=1, disabled=True)
    assert res.found is False
    assert res.reason == "camera disabled"
    assert not svc.nudge.called


def test_abort_stops_the_sweep_mid_flight():
    # request_abort() cancels an in-flight sweep; the flag is cleared at entry
    # so a stale abort cannot prevent the next search from ever running.
    res, svc = _run(detect_at_stop=None, abort_at_stop=2)
    assert res.reason == "aborted"
    assert res.stops_visited < search.MAX_STOPS


def test_a_stale_abort_does_not_block_the_next_search():
    search.request_abort()
    res, _svc = _run(detect_at_stop=1)
    assert res.found is True


# --- Task F / F7: the search restores the posture and honours confidence ---


def test_the_sweep_restores_the_remembered_posture_first():
    """A sweep is the one consumer that provably needs more than yaw.

    It steps the head across up to MAX_STOPS bearings; with the pitch left
    aimed at the desk it sweeps the desk MAX_STOPS times and reports nobody
    there — `user_bearing`'s own warning, applied to the consumer that sweeps
    by definition.
    """
    _res, svc = _run(
        bearing=40.0,
        pose={"base_yaw.pos": 40.0, "base_pitch.pos": -12.0, "wrist_pitch.pos": -30.0},
    )
    assert svc.holds, "no posture was restored before sweeping"
    restored = svc.holds[0]
    assert "wrist_pitch.pos" in restored or "base_pitch.pos" in restored, restored


def test_a_low_confidence_bearing_does_not_seed_the_sweep():
    """A seed is an ordering hint, but an estimate about to be dropped is not
    one — start from where the head already points instead."""
    from hal.drivers.tracking import aim

    res, svc = _run(bearing=120.0, confidence=aim.MIN_BEARING_CONFIDENCE - 0.05)
    assert svc.holds == [], "a bearing below the floor must not move the head"
    assert res.stops_visited >= 1


def test_a_confident_bearing_still_seeds_the_sweep():
    from hal.drivers.tracking import aim

    _res, svc = _run(bearing=40.0, confidence=aim.MIN_BEARING_CONFIDENCE + 0.05)
    assert svc.holds, "a bearing above the floor should be used"


def test_no_estimate_falls_back_to_where_the_head_points():
    _res, svc = _run(bearing=None)
    assert svc.holds == []


def test_a_failed_posture_restore_still_sweeps():
    """Sweeping from the wrong pitch beats not sweeping at all."""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    svc = _FakeSvc()
    svc.move_and_hold = mock.Mock(side_effect=RuntimeError("servo busy"))
    det = mock.Mock()
    det.detect = mock.Mock(return_value=None)
    est = mock.Mock(bearing_deg=40.0, confidence=0.9)
    est.pose = {"base_yaw.pos": 40.0, "wrist_pitch.pos": -30.0}

    with (
        mock.patch.object(state, "camera_capture", _FakeCap(frame)),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", False, create=True),
        mock.patch.object(search.time, "sleep"),
        mock.patch("hal.drivers.tracking.user_bearing.read_estimate", return_value=est),
    ):
        res = search.search_for_subject(detector=det)

    assert res.stops_visited >= 1, "a failed restore must not abort the search"
