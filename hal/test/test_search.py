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
    # The idle recording's first frame, which the animation service holds. The
    # sweep rests on it when there is no bearing to seed from.
    IDLE_BASELINE = {
        "base_yaw.pos": 3.0, "base_pitch.pos": 29.8, "elbow_pitch.pos": 27.1,
        "wrist_pitch.pos": -61.7, "wrist_roll.pos": 8.2,
    }

    def __init__(self, idle_baseline=None):
        self.yaw = 0.0
        self.roll = 0.0
        self.holds = []            # absolute poses restored before the sweep
        self._idle_baseline = (
            dict(self.IDLE_BASELINE) if idle_baseline is None else idle_baseline
        )
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
            "wrist_roll.pos": self.roll,
        })

    def get_joint_names(self):
        return ["base_yaw.pos", "base_pitch.pos", "elbow_pitch.pos",
                "wrist_pitch.pos", "wrist_roll.pos"]

    def move_and_hold(self, target, duration=None):
        self.holds.append(dict(target))
        if "base_yaw.pos" in target:
            self.yaw = float(target["base_yaw.pos"])
        if "wrist_roll.pos" in target:
            self.roll = float(target["wrist_roll.pos"])

    def _nudge(self, y, p, d, cur, pol):
        self.yaw += y
        return {"base_yaw.pos": self.yaw}


def _run(detect_at_stop=None, bearing=None, disabled=False, abort_at_stop=None,
         confidence=0.9, pose=None, idle_baseline=None):
    """detect_at_stop: 1-based stop index at which a subject appears."""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    svc = _FakeSvc(idle_baseline=idle_baseline)
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


def test_a_low_confidence_bearing_is_not_used_to_seed_the_sweep():
    """An estimate about to be dropped is not an ordering hint."""
    from hal.drivers.tracking import aim

    res, svc = _run(bearing=120.0, confidence=aim.MIN_BEARING_CONFIDENCE - 0.05)
    seeded_from_bearing = [h for h in svc.holds if h.get("base_yaw.pos") == 120.0]
    assert seeded_from_bearing == [], "a bearing below the floor must not aim the head"
    assert res.stops_visited >= 1


def test_no_bearing_rests_on_the_idle_pose_before_sweeping():
    """Sweeping from wherever the arm happens to be finds nothing.

    A loop that has been walking the head around does not leave it in a pose
    anyone chose, and a sweep from a camera aimed at the desk is thorough about
    the wrong hemisphere. The idle baseline is by construction a pose the lamp
    is designed to rest in — device-checked, it looks out at head height — so
    the "not aimed at the floor" guarantee comes from the pose, not from a
    separate pitch check.
    """
    _res, svc = _run(bearing=None)
    rested = [h for h in svc.holds if h.get("base_pitch.pos") == 29.8]
    assert rested, f"expected a rest on the idle pose, got {svc.holds[:3]}"


def test_with_no_idle_pose_either_the_sweep_starts_where_it_stands():
    """The last resort. A device with neither memory still sweeps rather than
    refusing — half a search beats none."""
    _res, svc = _run(bearing=None, idle_baseline={})
    assert [h for h in svc.holds if "base_pitch.pos" in h] == []


def test_a_confident_bearing_still_seeds_the_sweep():
    from hal.drivers.tracking import aim

    _res, svc = _run(bearing=40.0, confidence=aim.MIN_BEARING_CONFIDENCE + 0.05)
    assert svc.holds, "a bearing above the floor should be used"


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


# --- the head looks around at each stop ----------------------------------------


def _rolls(svc):
    """Every wrist_roll angle the sweep commanded, in order."""
    return [h["wrist_roll.pos"] for h in svc.holds if "wrist_roll.pos" in h]


def test_each_stop_looks_left_centre_and_right():
    """Turning the whole lamp reads as a camera on a turntable; turning the head
    at a fixed body reads as something looking around. Both cover ground, only
    one of them looks alive."""
    _res, svc = _run(bearing=None)
    rolls = _rolls(svc)

    assert -45.0 in rolls and 0.0 in rolls and 45.0 in rolls
    first = rolls.index(-45.0)
    assert rolls[first:first + 3] == [-45.0, 0.0, 45.0], (
        f"expected left -> centre -> right, got {rolls[:6]}"
    )


def test_the_head_returns_to_centre_before_the_base_turns():
    """At roll 0 the camera looks along base_yaw, which is what makes a yaw stop
    mean what the stop list says. Turning the base with the head still cranked
    45 deg over would aim every later stop somewhere other than where it claims.
    """
    _res, svc = _run(bearing=None)
    rolls = _rolls(svc)

    # After each right-look there must be a return to centre before the next
    # left-look begins the following stop.
    for i, roll in enumerate(rolls):
        if roll == 45.0 and any(r == -45.0 for r in rolls[i + 1:]):
            nxt = rolls[i + 1:]
            assert nxt[0] == 0.0, f"base turned with the head still at +45: {rolls}"


def test_a_subject_found_mid_look_stops_the_sweep_there():
    """The sweep ends on the first subject seen — that is the whole contract,
    and adding a second axis must not make it keep looking past them."""
    res, svc = _run(detect_at_stop=2, bearing=None)
    assert res.found
    assert res.stops_visited == 2, "it kept looking after finding someone"


def test_looking_around_multiplies_the_stops_not_the_yaw_positions():
    """Three looks per yaw stop, so coverage comes from the head rather than
    from turning the body more often."""
    res, svc = _run(bearing=None)
    # The first yaw stop needs no move — the seed already left the head there —
    # so the body turns once fewer than the number of stops it visits.
    yaw_stops = svc.nudge.call_count + 1
    assert res.stops_visited == yaw_stops * len(search.ROLL_STOPS), (
        f"{res.stops_visited} stops from {yaw_stops} yaw positions"
    )
