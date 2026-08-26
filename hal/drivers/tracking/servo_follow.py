"""Servo follow worker for tracking — glides the arm toward a published goal.

The vision loop publishes an absolute 4-joint goal; a dedicated worker thread
glides toward it with a SmoothDamp critically-damped follower (ease-in /
ease-out), coalescing tiny intermediate setpoint changes. Decoupling the two
keeps the ViT tracker updating at full speed instead of ~halving fps waiting
for each servo command to finish. The follower owns the current-position state
for all 4 joints.
"""

import logging
import threading
import time
from typing import Dict, Optional

from hal.drivers.tracking import constants as C
from hal.drivers.tracking.filters import smooth_damp

logger = logging.getLogger(__name__)

JOINTS = ("base_yaw.pos", "base_pitch.pos", "elbow_pitch.pos", "wrist_pitch.pos")


# Joint delta that produces one degree of DOWNWARD camera tilt. The three pitch
# axes are parallel, so each contributes about 1:1 and the contributions simply
# add — which is what lets a correction be split across them at all. The elbow
# motor's positive direction is reversed in hardware, hence its -1.
PITCH_AXIS_SIGN = {
    "base_pitch.pos":  1.0,
    "elbow_pitch.pos": C.ELBOW_PITCH_SIGN,
    "wrist_pitch.pos": 1.0,
}
PITCH_AXIS_WEIGHT = {
    "base_pitch.pos":  C.PITCH_WEIGHT_BASE,
    "elbow_pitch.pos": C.PITCH_WEIGHT_ELBOW,
    "wrist_pitch.pos": C.PITCH_WEIGHT_WRIST,
}
# Absolute stops, as a backstop for a joint already parked outside its measured
# travel — the allocator never drives one further out than it found it.
PITCH_AXIS_HARD = {
    "base_pitch.pos":  (C.BASE_PITCH_MIN, C.BASE_PITCH_MAX),
    "elbow_pitch.pos": (C.ELBOW_PITCH_MIN, C.ELBOW_PITCH_MAX),
    "wrist_pitch.pos": (C.WRIST_PITCH_MIN, C.WRIST_PITCH_MAX),
}


def distribute_pitch(
    current: Dict[str, float],
    pitch_deg: float,
    travel_min: Optional[Dict[str, float]] = None,
    travel_max: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """Spread one camera-pitch correction across the three pitch joints.

    Allocated against the travel each joint actually HAS in the direction being
    asked for, not by fixed weights alone. The weights still choose who goes
    first; a joint with no room contributes nothing and its share passes to a
    joint that can move.

    That distinction is the whole point, because the arm is badly asymmetric
    (see PITCH_TRAVEL_* in constants). Looking up, wrist_pitch has about 2 deg
    before it stalls; looking down it has about 65. A fixed weight has to pick
    one number for both, so it either wastes the wrist entirely or commands it
    into a stop. Gaze did the latter: it spent every correction on the wrist,
    `/servo/move` accepted the target unclamped, the servo answered
    `position error 14.6 deg (target=-49.0, actual=-34.4)`, and the head never
    lifted while the log cheerfully announced a correction every 10 seconds.

    Device-measured the same day with base and wrist pinned and only elbow
    moving: elbow +1.6 framed the desk, +54.8 framed the ceiling. Elbow is the
    strongest and freest joint upward, which is what its 0.90 weight encodes.

    travel_min / travel_max narrow a joint's usable range for this call only.
    Callers that can observe a joint failing to arrive use them to route around
    it — the measured limits below are static, but what a servo will actually
    deliver is not: the same elbow stalled at +17.4 and, after 60s of rest,
    reached +44 three times running.

    Shared with `gaze._maybe_pitch` on purpose — two copies of the joint model
    is how the wrist bug survived as long as it did.
    """
    return _allocate(
        current, pitch_deg, PITCH_AXIS_SIGN, PITCH_AXIS_WEIGHT, PITCH_AXIS_HARD,
        C.PITCH_TRAVEL_MIN, C.PITCH_TRAVEL_MAX, travel_min, travel_max,
    )


def _allocate(current, want, signs, weights_by_joint, hard,
              soft_min, soft_max, travel_min, travel_max):
    """Spend `want` degrees of camera rotation across a set of parallel joints.

    Shared by pitch and yaw because the problem is identical once the axis is
    chosen: honour the weights first, then give whatever a saturated joint could
    not absorb to anyone with room left. Only the joints, signs and limits
    differ, so duplicating this for yaw would have meant two copies of the one
    rule that has already been fixed twice.
    """
    target = {j: float(current.get(j, 0.0)) for j in signs}
    if abs(float(want)) < 1e-9:
        return target
    positive = float(want) > 0.0

    def room(joint: str) -> float:
        """Degrees this joint can still give in the requested direction."""
        lo, hi = hard[joint]
        lo = max(lo, soft_min.get(joint, lo), (travel_min or {}).get(joint, lo))
        hi = min(hi, soft_max.get(joint, hi), (travel_max or {}).get(joint, hi))
        rising = (signs[joint] > 0.0) == positive
        return max(0.0, (hi - target[joint]) if rising else (target[joint] - lo))

    budget = abs(float(want))
    # Pass 1 honours the weights. Pass 2 hands whatever a saturated joint could
    # not take to anyone with room left — which is how wrist_pitch earns its
    # keep on downward corrections despite a first-choice weight of 0.0.
    for weights in (weights_by_joint, {j: 1.0 for j in signs}):
        if budget <= 1e-9:
            break
        pool = [j for j in signs if weights[j] > 0.0 and room(j) > 1e-9]
        total = sum(weights[j] for j in pool)
        if not pool or total <= 0.0:
            continue
        share_of = budget
        for joint in pool:
            if budget <= 1e-9:
                break
            give = min(share_of * weights[joint] / total, room(joint), budget)
            target[joint] += signs[joint] * (give if positive else -give)
            budget -= give
    return target


# --- panning -------------------------------------------------------------------

# Both joints pan the same way: increasing either turns the camera right, so a
# face on the right (dx > 0) is corrected by increasing both. Device-verified by
# capture, not inferred — see YAW_WEIGHT_* in constants.
YAW_AXIS_SIGN = {"base_yaw.pos": 1.0, "wrist_roll.pos": 1.0}
YAW_AXIS_WEIGHT = {
    "base_yaw.pos":   C.YAW_WEIGHT_BASE,
    "wrist_roll.pos": C.YAW_WEIGHT_ROLL,
}
YAW_AXIS_HARD = {
    "base_yaw.pos":   (C.YAW_MIN, C.YAW_MAX),
    "wrist_roll.pos": (C.WRIST_ROLL_MIN, C.WRIST_ROLL_MAX),
}


def distribute_yaw(
    current: Dict[str, float],
    yaw_deg: float,
    travel_min: Optional[Dict[str, float]] = None,
    travel_max: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """Spread one horizontal correction across base_yaw and wrist_roll.

    The mirror of distribute_pitch, and simpler: both joints pan the same way,
    neither fights gravity, and wrist_roll reached every target from -59 to +59
    cleanly on the device. They are also on nearly the same normalised scale
    (12.0 vs 11.5 counts per unit), so adding their contributions 1:1 is sound
    here in a way it is not for the pitch joints.

    base_yaw leads at 0.75. Turning the base is what reads as "it looked at me",
    and `user_bearing` stores the bearing AS base_yaw — aiming mostly with the
    wrist would leave the remembered bearing describing a pose never held.
    """
    return _allocate(
        current, yaw_deg, YAW_AXIS_SIGN, YAW_AXIS_WEIGHT, YAW_AXIS_HARD,
        C.YAW_TRAVEL_MIN, C.YAW_TRAVEL_MAX, travel_min, travel_max,
    )


class ServoFollower:
    """Owns the servo goal, the follow worker thread, and the arm's current
    tracked position for the 4 tracking joints."""

    def __init__(self):
        self._lock = threading.Lock()
        self._goal: Optional[dict] = None
        self._thread: Optional[threading.Thread] = None
        self._yaw = 0.0
        self._base_pitch = 0.0
        self._elbow_pitch = 0.0
        self._wrist_pitch = 0.0
        # Motion profile (pursuit by default; the vision loop switches to the
        # saccade profile on large offsets). Read by the worker every tick.
        self._smooth_time = C.SERVO_SMOOTH_TIME
        self._max_speed_dps = C.SERVO_MAX_SPEED_DPS

    def set_profile(self, smooth_time: float, max_speed_dps: float) -> None:
        """Switch the follower's motion profile (pursuit ↔ saccade)."""
        with self._lock:
            self._smooth_time = smooth_time
            self._max_speed_dps = max_speed_dps

    # --- position state ---

    def read_initial_positions(self, animation_service) -> None:
        """Read servo positions from the bus once; track internally after this."""
        try:
            from hal.drivers.motors.animation_service import _motor_positions_from_bus
            with animation_service.bus_lock:
                init_pos = _motor_positions_from_bus(animation_service.robot)
            with self._lock:
                self._yaw = init_pos.get("base_yaw.pos", 0.0)
                self._base_pitch = init_pos.get("base_pitch.pos", 0.0)
                self._elbow_pitch = init_pos.get("elbow_pitch.pos", 0.0)
                self._wrist_pitch = init_pos.get("wrist_pitch.pos", 0.0)
        except Exception:
            with self._lock:
                self._yaw = self._base_pitch = self._elbow_pitch = self._wrist_pitch = 0.0

    def _positions_locked(self) -> Dict[str, float]:
        return {
            "base_yaw.pos":    self._yaw,
            "base_pitch.pos":  self._base_pitch,
            "elbow_pitch.pos": self._elbow_pitch,
            "wrist_pitch.pos": self._wrist_pitch,
        }

    def positions(self) -> Dict[str, float]:
        with self._lock:
            return self._positions_locked()

    # --- goal management ---

    def set_goal(self, target: dict) -> None:
        """Publish a new absolute servo goal for the worker (non-blocking)."""
        with self._lock:
            self._goal = dict(target)

    def seed_goal_current(self) -> None:
        """Seed the goal with the current pose so the worker holds position
        until the vision loop publishes corrections."""
        with self._lock:
            self._goal = self._positions_locked()

    def hold(self) -> None:
        """Retarget the worker to the current pose so it settles in place.

        Without this, entering a hold state (low confidence, WAIT-YOLO,
        BLOAT-HOLD) only stopped publishing NEW goals — the worker kept gliding
        toward the last stale goal, so the arm visibly chased a ghost for a
        beat after the lock had already gone bad.
        """
        with self._lock:
            if self._goal is None:
                return
            self._goal = self._positions_locked()

    def clear_goal(self) -> None:
        with self._lock:
            self._goal = None

    @staticmethod
    def _tick_dt(now: float, previous: float) -> float:
        """Return the elapsed follower time, bounded after scheduler stalls."""
        return min(C.SERVO_SUBSTEP_MAX_DT_S, max(0.0, now - previous))

    @staticmethod
    def _should_write(step: Dict[str, float], last_sent: Dict[str, float],
                      force: bool = False) -> bool:
        """Avoid writes which quantize to the same servo encoder target."""
        if force:
            return any(abs(step[k] - last_sent[k]) > 0.0 for k in JOINTS)
        return any(
            abs(step[k] - last_sent[k]) >= C.SERVO_COMMAND_MIN_DELTA
            for k in JOINTS
        )

    # --- vision-loop commands ---

    def command_pid(self, yaw_step: float, pitch_correction: float) -> None:
        """Apply PID outputs. yaw → base_yaw. pitch → distributed across base/elbow/wrist.

        Pitch sign — empirical evidence over time:
          2026-05-13: claimed base+ = UP, elbow+ = DOWN, wrist+ = UP, code used
                      `wrist - pitch_correction` to look UP when dy<0.
          2026-05-14: log shows face dy=-180 → pid pitch=-5 → code wrote
                      wrist -67→-7 (INCREASE), and the device visibly tilted DOWN.
                      So wrist+ is actually DOWN at the poses we encounter, and
                      the sign was inverted. Flipped to `wrist + pitch_correction`
                      so the camera now moves toward dy (per the long-standing
                      memory rule pitch_deg = dy*k applied as wrist_new = wrist + pitch_deg).
        """
        with self._lock:
            cur = self._positions_locked()
        target = dict(distribute_pitch(cur, pitch_correction))
        target["base_yaw.pos"] = max(C.YAW_MIN, min(C.YAW_MAX, cur["base_yaw.pos"] + yaw_step))
        # Warn loudly when an axis has saturated against its mechanical limit and
        # the PID is still demanding more travel in that direction — camera
        # physically can't follow further; only re-centering the device helps.
        if abs(yaw_step) >= 0.1 and (
            (yaw_step < 0 and cur["base_yaw.pos"] <= C.YAW_MIN + 0.5) or
            (yaw_step > 0 and cur["base_yaw.pos"] >= C.YAW_MAX - 0.5)
        ):
            logger.warning("[saturation] yaw at limit %.1f° but PID still demanding %.2f° — recenter device",
                           cur["base_yaw.pos"], yaw_step)
        if abs(pitch_correction) >= 0.1 and C.PITCH_WEIGHT_WRIST > 0 and (
            (pitch_correction > 0 and cur["wrist_pitch.pos"] >= C.WRIST_PITCH_MAX - 0.5) or
            (pitch_correction < 0 and cur["wrist_pitch.pos"] <= C.WRIST_PITCH_MIN + 0.5)
        ):
            logger.warning("[saturation] wrist at limit %.1f° but PID still demanding pitch=%.2f° — recenter device",
                           cur["wrist_pitch.pos"], pitch_correction)
        # Non-blocking: hand the target to the servo worker and return. The
        # worker glides toward it while the vision loop grabs the next frame.
        self.set_goal(target)

    def sweep_yaw(self, delta_deg: float) -> None:
        """Search sweep: nudge base_yaw by delta while holding the other joints
        (used while the tracker has lost the target)."""
        with self._lock:
            goal = self._positions_locked()
            goal["base_yaw.pos"] = max(C.YAW_MIN, min(C.YAW_MAX, self._yaw + delta_deg))
            self._goal = goal

    # --- worker lifecycle ---

    def start(self, animation_service, running: threading.Event) -> None:
        """Spawn the follow worker; it runs until `running` is cleared."""
        self._thread = threading.Thread(
            target=self._worker, args=(animation_service, running),
            daemon=True, name="servo-follow-worker",
        )
        self._thread.start()

    def join(self, timeout: float) -> None:
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("[servo-worker] refused to exit within %.0fs", timeout)

    def _worker(self, animation_service, running: threading.Event) -> None:
        """Continuously glide servos toward the latest goal, decoupled from the
        vision loop.

        Each iteration advances every joint toward the latest goal with the
        SmoothDamp critically-damped follower (ease-in/ease-out). A bus command
        is sent only once the accumulated setpoint differs meaningfully from
        the last command, reducing redundant clicks while preserving the smooth
        velocity profile. Each joint carries its own velocity, so when a fresh
        goal arrives mid-move the follower retargets without a restart jerk.
        """
        # Own the body for as long as this thread writes to it. The tracker's
        # own flag spans `_track_loop`, which this worker outlives — and in that
        # gap the lock read free while this loop was still writing every joint
        # at 30fps, so gaze happily made corrections that were overwritten on
        # the next frame and then reported the servo as broken.
        acquire = getattr(animation_service, "acquire_body", None)
        release = getattr(animation_service, "release_body", None)
        if acquire:
            acquire()
        try:
            self._follow(animation_service, running)
        finally:
            if release:
                release()

    def _follow(self, animation_service, running: threading.Event) -> None:
        """The follow loop itself. Split out so ownership is released on EVERY
        exit path, including an exception mid-loop."""
        idle_sleep = 0.01
        vel = {k: 0.0 for k in JOINTS}   # per-joint SmoothDamp velocity (deg/s)
        last_sent = self.positions()
        previous_tick = time.perf_counter()
        while running.is_set():
            # Camera freeze: a snapshot/look consumer wants a sharp frame.
            # The animation loop honors _frozen; without this check the worker
            # kept writing right through the freeze (the "snapshot blurry
            # during tracking" bug). Goal is kept — following resumes as soon
            # as the flag clears. Bleed velocity so the ease-out doesn't
            # overshoot from stale momentum on resume.
            if animation_service.is_frozen:
                for k in JOINTS:
                    vel[k] = 0.0
                previous_tick = time.perf_counter()
                time.sleep(idle_sleep)
                continue
            with self._lock:
                goal = dict(self._goal) if self._goal is not None else None
                cur = self._positions_locked()
                smooth_time = self._smooth_time
                max_speed = self._max_speed_dps
            if goal is None:
                previous_tick = time.perf_counter()
                time.sleep(idle_sleep)
                continue
            now = time.perf_counter()
            dt = self._tick_dt(now, previous_tick)
            previous_tick = now
            max_delta = max(abs(goal[k] - cur[k]) for k in JOINTS)
            max_vel = max(abs(v) for v in vel.values())
            # Settled AND stopped → idle. Keep ticking while residual velocity
            # bleeds off so the ease-out completes instead of snapping.
            if max_delta < 0.05 and max_vel < 0.5:
                for k in JOINTS:
                    vel[k] = 0.0
                # The previous write may have been intentionally suppressed.
                # Send the final target once so the motor receives the complete
                # requested goal rather than stopping at the last coalesced step.
                if self._should_write(goal, last_sent, force=True):
                    try:
                        with animation_service.bus_lock:
                            animation_service.robot.send_action(goal)
                        last_sent = dict(goal)
                    except Exception as e:
                        logger.warning("[servo-worker] final send failed: %s", e)
                time.sleep(idle_sleep)
                continue
            step = {}
            for k in JOINTS:
                step[k], vel[k] = smooth_damp(
                    cur[k], goal[k], vel[k],
                    smooth_time, dt, max_speed,
                )
            if not self._should_write(step, last_sent):
                with self._lock:
                    self._yaw         = step["base_yaw.pos"]
                    self._base_pitch  = step["base_pitch.pos"]
                    self._elbow_pitch = step["elbow_pitch.pos"]
                    self._wrist_pitch = step["wrist_pitch.pos"]
                time.sleep(C.SERVO_SUBSTEP_SLEEP)
                continue
            try:
                with animation_service.bus_lock:
                    animation_service.robot.send_action(step)
            except Exception as e:
                logger.warning("[servo-worker] send failed: %s", e)
                time.sleep(idle_sleep)
                continue
            last_sent = dict(step)
            with self._lock:
                self._yaw         = step["base_yaw.pos"]
                self._base_pitch  = step["base_pitch.pos"]
                self._elbow_pitch = step["elbow_pitch.pos"]
                self._wrist_pitch = step["wrist_pitch.pos"]
            time.sleep(C.SERVO_SUBSTEP_SLEEP)
