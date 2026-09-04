"""Recording playback respects SAFETY.md motion.max_speed (#219)."""
from hal.drivers.motors.recording_timing import stretch_timeline
from hal.safety.policy import MotionBounds, SafetyPolicy


def _policy(max_speed):
    return SafetyPolicy(schema="v1", motion=MotionBounds(max_speed=max_speed, stop_always=True))


# One 30 deg step in 0.1s = 300 deg/s: over the servo limit and over any policy.
TIMES = [0.0, 0.1]
FRAMES = [{"base_yaw": 0.0}, {"base_yaw": 30.0}]


def test_no_policy_uses_servo_limit():
    # 30/250 = 0.12s
    assert round(stretch_timeline(TIMES, FRAMES)[-1], 3) == 0.12


def test_declared_ceiling_stretches_further():
    # 30/120 = 0.25s — the declared bound is lower, so it wins.
    assert round(stretch_timeline(TIMES, FRAMES, _policy(120))[-1], 3) == 0.25


def test_ceiling_above_hardware_does_not_loosen():
    assert round(stretch_timeline(TIMES, FRAMES, _policy(400))[-1], 3) == 0.12


def test_slow_segment_untouched():
    times, frames = [0.0, 1.0], [{"base_yaw": 0.0}, {"base_yaw": 10.0}]
    assert stretch_timeline(times, frames, _policy(120)) == times
