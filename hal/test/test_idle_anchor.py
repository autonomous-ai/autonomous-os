"""Tests for re-centring the idle loop on a pose that can see the user.

An idle recording is absolute on every joint and loops forever, so within a
cycle of any correction it walks the camera back to the pose it was recorded at
— on a desk, the keyboard. Anchoring shifts the loop without changing its shape.
"""

import pytest

from hal.drivers.motors.animation_service import AnimationService


@pytest.fixture
def svc():
    s = AnimationService.__new__(AnimationService)
    s.idle_recording = "idle"
    s._idle_anchor = {}
    s._idle_baseline = {}
    s._current_recording = "idle"
    return s


def test_without_an_anchor_frames_play_exactly_as_recorded(svc):
    frame = {"wrist_pitch.pos": -80.0, "base_yaw.pos": 5.0}
    assert svc._anchor_action(frame) is frame


def test_an_anchored_joint_is_shifted_by_its_displacement(svc):
    svc._idle_baseline = {"wrist_pitch.pos": -80.0}
    svc.set_idle_anchor({"wrist_pitch.pos": -50.0})
    out = svc._anchor_action({"wrist_pitch.pos": -78.0, "base_yaw.pos": 5.0})
    # The frame sat 2 degrees off its own baseline; it must still sit 2 degrees
    # off the new centre — the loop keeps its shape.
    assert out["wrist_pitch.pos"] == pytest.approx(-48.0)


def test_joints_that_were_not_anchored_are_left_alone(svc):
    svc._idle_baseline = {"wrist_pitch.pos": -80.0, "base_yaw.pos": 0.0}
    svc.set_idle_anchor({"wrist_pitch.pos": -50.0})
    out = svc._anchor_action({"wrist_pitch.pos": -80.0, "base_yaw.pos": 12.0})
    assert out["base_yaw.pos"] == 12.0


def test_the_swing_of_the_loop_is_preserved_not_flattened(svc):
    """Anchoring must move the centre, never damp the motion."""
    svc._idle_baseline = {"wrist_pitch.pos": -80.0}
    svc.set_idle_anchor({"wrist_pitch.pos": -40.0})
    lo = svc._anchor_action({"wrist_pitch.pos": -88.0})["wrist_pitch.pos"]
    hi = svc._anchor_action({"wrist_pitch.pos": -72.0})["wrist_pitch.pos"]
    assert hi - lo == pytest.approx(16.0)


def test_only_the_idle_recording_is_anchored(svc):
    """Emotion recordings are free to fling the head anywhere."""
    svc._idle_baseline = {"wrist_pitch.pos": -80.0}
    svc.set_idle_anchor({"wrist_pitch.pos": -50.0})
    svc._current_recording = "happy_wiggle"
    frame = {"wrist_pitch.pos": -80.0}
    assert svc._anchor_action(frame) is frame


def test_clearing_the_anchor_restores_the_recorded_pose(svc):
    svc._idle_baseline = {"wrist_pitch.pos": -80.0}
    svc.set_idle_anchor({"wrist_pitch.pos": -50.0})
    svc.set_idle_anchor(None)
    frame = {"wrist_pitch.pos": -80.0}
    assert svc._anchor_action(frame) is frame


def test_a_joint_with_no_baseline_yet_is_not_guessed_at(svc):
    """Before the recording has loaded there is nothing to measure from."""
    svc.set_idle_anchor({"wrist_pitch.pos": -50.0})
    out = svc._anchor_action({"wrist_pitch.pos": -80.0})
    assert out["wrist_pitch.pos"] == -80.0
