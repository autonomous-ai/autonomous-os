"""Tests for re-centring the idle loop on a pose that can see the user.

An idle recording is absolute on every joint and loops forever, so within a
cycle of any correction it walks the camera back to the pose it was recorded at
— on a desk, the keyboard. Anchoring shifts the loop without changing its shape.
"""

import pytest

from hal.drivers.motors.animation_service import (
    IDLE_ANCHOR_SLEW_DPS,
    AnimationService,
)


@pytest.fixture
def svc():
    s = AnimationService.__new__(AnimationService)
    s.idle_recording = "idle"
    s._idle_anchor = {}
    s._idle_anchor_target = {}
    s._idle_baseline = {}
    s._current_recording = "idle"
    s.fps = 20
    return s


def settle(svc, frames: int = 400) -> None:
    """Run the anchor slew to completion — most tests are about where the
    anchor lands, not how it travels."""
    for _ in range(frames):
        svc._advance_idle_anchor()


def test_without_an_anchor_frames_play_exactly_as_recorded(svc):
    frame = {"wrist_pitch.pos": -80.0, "base_yaw.pos": 5.0}
    assert svc._anchor_action(frame) is frame


def test_an_anchored_joint_is_shifted_by_its_displacement(svc):
    svc._idle_baseline = {"wrist_pitch.pos": -80.0}
    svc.set_idle_anchor({"wrist_pitch.pos": -50.0})
    settle(svc)
    out = svc._anchor_action({"wrist_pitch.pos": -78.0, "base_yaw.pos": 5.0})
    # The frame sat 2 degrees off its own baseline; it must still sit 2 degrees
    # off the new centre — the loop keeps its shape.
    assert out["wrist_pitch.pos"] == pytest.approx(-48.0)


def test_joints_that_were_not_anchored_are_left_alone(svc):
    svc._idle_baseline = {"wrist_pitch.pos": -80.0, "base_yaw.pos": 0.0}
    svc.set_idle_anchor({"wrist_pitch.pos": -50.0})
    settle(svc)
    out = svc._anchor_action({"wrist_pitch.pos": -80.0, "base_yaw.pos": 12.0})
    assert out["base_yaw.pos"] == 12.0


def test_the_swing_of_the_loop_is_preserved_not_flattened(svc):
    """Anchoring must move the centre, never damp the motion."""
    svc._idle_baseline = {"wrist_pitch.pos": -80.0}
    svc.set_idle_anchor({"wrist_pitch.pos": -40.0})
    settle(svc)
    lo = svc._anchor_action({"wrist_pitch.pos": -88.0})["wrist_pitch.pos"]
    hi = svc._anchor_action({"wrist_pitch.pos": -72.0})["wrist_pitch.pos"]
    assert hi - lo == pytest.approx(16.0)


def test_only_the_idle_recording_is_anchored(svc):
    """Emotion recordings are free to fling the head anywhere."""
    svc._idle_baseline = {"wrist_pitch.pos": -80.0}
    svc.set_idle_anchor({"wrist_pitch.pos": -50.0})
    settle(svc)
    svc._current_recording = "happy_wiggle"
    frame = {"wrist_pitch.pos": -80.0}
    assert svc._anchor_action(frame) is frame


def test_clearing_the_anchor_restores_the_recorded_pose(svc):
    svc._idle_baseline = {"wrist_pitch.pos": -80.0}
    svc.set_idle_anchor({"wrist_pitch.pos": -50.0})
    settle(svc)
    svc.set_idle_anchor(None)
    settle(svc)
    frame = {"wrist_pitch.pos": -80.0}
    assert svc._anchor_action(frame) is frame


def test_a_joint_with_no_baseline_yet_is_not_guessed_at(svc):
    """Before the recording has loaded there is nothing to measure from."""
    svc.set_idle_anchor({"wrist_pitch.pos": -50.0})
    settle(svc)
    out = svc._anchor_action({"wrist_pitch.pos": -80.0})
    assert out["wrist_pitch.pos"] == -80.0


def test_a_re_aim_is_travelled_not_teleported(svc):
    """The bug: a new anchor landed on the next frame at full size.

    Device-observed gaze re-aim — wrist_pitch anchor -4.9 -> +36.7 — used to
    move the arm 41 degrees inside one 50 ms frame.
    """
    svc._idle_baseline = {"wrist_pitch.pos": -4.9}
    svc.set_idle_anchor({"wrist_pitch.pos": 36.7})
    step = IDLE_ANCHOR_SLEW_DPS / svc.fps

    first = svc._anchor_action({"wrist_pitch.pos": -4.9})["wrist_pitch.pos"]
    assert first == pytest.approx(-4.9 + step)

    second = svc._anchor_action({"wrist_pitch.pos": -4.9})["wrist_pitch.pos"]
    assert second - first == pytest.approx(step)


def test_the_slew_reaches_the_requested_anchor_exactly(svc):
    """Bounded travel must still arrive, and must not overshoot past it."""
    svc._idle_baseline = {"wrist_pitch.pos": -4.9}
    svc.set_idle_anchor({"wrist_pitch.pos": 36.7})
    settle(svc)
    assert svc._idle_anchor["wrist_pitch.pos"] == pytest.approx(36.7)


def test_no_frame_of_the_slew_exceeds_the_rate_limit(svc):
    """Every step, including the last partial one, stays within the bound."""
    svc._idle_baseline = {"wrist_pitch.pos": 0.0}
    svc.set_idle_anchor({"wrist_pitch.pos": 41.6})
    step = IDLE_ANCHOR_SLEW_DPS / svc.fps
    previous = 0.0
    for _ in range(100):
        svc._advance_idle_anchor()
        current = svc._idle_anchor["wrist_pitch.pos"]
        assert abs(current - previous) <= step + 1e-9
        previous = current


def test_a_retarget_mid_slew_is_followed_from_where_it_got_to(svc):
    """Gaze re-aims while an earlier re-aim is still travelling."""
    svc._idle_baseline = {"wrist_pitch.pos": 0.0}
    svc.set_idle_anchor({"wrist_pitch.pos": 40.0})
    for _ in range(5):
        svc._advance_idle_anchor()
    partway = svc._idle_anchor["wrist_pitch.pos"]
    assert 0.0 < partway < 40.0

    svc.set_idle_anchor({"wrist_pitch.pos": -20.0})
    step = IDLE_ANCHOR_SLEW_DPS / svc.fps
    svc._advance_idle_anchor()
    # Turns around from where it had got to, not from the old target.
    assert svc._idle_anchor["wrist_pitch.pos"] == pytest.approx(partway - step)
    settle(svc)
    assert svc._idle_anchor["wrist_pitch.pos"] == pytest.approx(-20.0)


def test_clearing_the_anchor_eases_back_instead_of_snapping(svc):
    svc._idle_baseline = {"wrist_pitch.pos": -80.0}
    svc.set_idle_anchor({"wrist_pitch.pos": -50.0})
    settle(svc)
    svc.set_idle_anchor(None)
    step = IDLE_ANCHOR_SLEW_DPS / svc.fps
    svc._advance_idle_anchor()
    assert svc._idle_anchor["wrist_pitch.pos"] == pytest.approx(-50.0 - step)
