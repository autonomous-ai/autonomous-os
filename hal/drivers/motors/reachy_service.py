"""Reachy Mini motion driver — wraps Pollen's `reachy_mini` SDK.

Satisfies the MotionService protocol (hal/drivers/motors/base.py) so
routes/servo.py works unchanged on Reachy Mini.

Architecture: Pollen ships a daemon on the robot's Pi that owns hardware I/O,
safety clamping, and the control loop; this driver is a thin client streaming
targets to it (REST/WS on localhost:8000). Unlike the feetech backend there is
no client-side animation event loop — the daemon holds poses by itself, so
several protocol methods (ensure_running, freeze) reduce to flags/no-ops.

Joint model exposed to HAL (degrees + millimeters, matching the deg-based
safety policy and route error checks; the SDK itself speaks radians + meters):

    head_x.pos / head_y.pos / head_z.pos      head translation, mm
    head_roll.pos / head_pitch.pos / head_yaw.pos   head rotation, deg
    body_yaw.pos                              body rotation, deg
    antenna_left.pos / antenna_right.pos      antennas, deg

DRAFT: written against the public reachy-mini SDK docs (v1.9) before hardware
arrival. Everything tagged TODO(spike) must be verified on the real robot.
"""
from __future__ import annotations

import logging
import math
import os
import threading
from typing import Any, Dict, List, Optional, Set

import numpy as np
from scipy.spatial.transform import Rotation

from reachy_mini import ReachyMini
from reachy_mini.utils import create_head_pose

# SDK motor ids, in get_current_joint_positions() head-list order.
_HEAD_MOTOR_IDS = (
    "body_rotation",
    "stewart_1", "stewart_2", "stewart_3", "stewart_4", "stewart_5", "stewart_6",
)

logger = logging.getLogger(__name__)

# --- Joint keys ---

_HEAD_KEYS = (
    "head_x.pos", "head_y.pos", "head_z.pos",
    "head_roll.pos", "head_pitch.pos", "head_yaw.pos",
)
_ANTENNA_KEYS = ("antenna_left.pos", "antenna_right.pos")
_BODY_KEY = "body_yaw.pos"
JOINT_KEYS: Set[str] = {*_HEAD_KEYS, *_ANTENNA_KEYS, _BODY_KEY}

# Daemon-enforced limits (the daemon clamps to the nearest valid pose; these
# are documented here for reference): head pitch/roll ±40°, head yaw ±180°,
# body yaw ±160°, head-vs-body yaw delta ≤ 65°.

# Aim table — same direction vocabulary as hal/presets AIM_PRESETS, values are
# Reachy head kinematics. Convention: +yaw = left, +pitch = up (ROS-style).
# TODO(spike): verify sign convention and tune angles on the real robot.
_AIM_TARGETS: Dict[str, Dict[str, float]] = {
    "center": {"head_yaw.pos": 0.0, "head_pitch.pos": 0.0, "head_z.pos": 0.0},
    "desk":   {"head_yaw.pos": 0.0, "head_pitch.pos": -30.0, "head_z.pos": 0.0},
    "wall":   {"head_yaw.pos": 0.0, "head_pitch.pos": 20.0, "head_z.pos": 0.0},
    "left":   {"head_yaw.pos": 40.0},
    "right":  {"head_yaw.pos": -40.0},
    "up":     {"head_pitch.pos": 30.0},
    "down":   {"head_pitch.pos": -35.0},
    "user":   {"head_yaw.pos": 0.0, "head_pitch.pos": 15.0},
}

# Default HF move library preloaded by the daemon.
_EMOTES_DATASET = "pollen-robotics/reachy-mini-emotions-library"

_MIN_MOVE_DURATION_S = 0.1


class ReachyMotionService:
    """MotionService implementation backed by the Pollen reachy_mini daemon."""

    def __init__(self, host: Optional[str] = None, port: Optional[int] = None):
        # HAL runs on the robot's own Pi, so the daemon is local by default.
        self._host = host or os.getenv("REACHY_DAEMON_HOST", "localhost")
        self._port = int(port or os.getenv("REACHY_DAEMON_PORT", "8000"))
        self._mini: Optional[ReachyMini] = None
        # The SDK client documents no thread-safety guarantee — one writer.
        self._lock = threading.RLock()
        # Last commanded targets; fallback state when the daemon can't be read.
        self._target: Dict[str, float] = {k: 0.0 for k in JOINT_KEYS}
        self._suppressed = False
        self._frozen = False
        self._moves = None          # lazy RecordedMoves loader (None=untried, False=failed)
        self._play_thread: Optional[threading.Thread] = None
        # Read by GET /servo alongside get_available_recordings().
        self._current_recording: Optional[str] = None

    # --- Lifecycle ---

    def start(self) -> None:
        with self._lock:
            # media_backend="no_media": HAL owns camera/audio via its own
            # drivers — the SDK must not grab them too.
            self._mini = ReachyMini(
                host=self._host, port=self._port, media_backend="no_media"
            )
            try:
                self._mini.wake_up()
            except Exception as e:
                logger.warning("[reachy] wake_up failed (continuing): %s", e)
        logger.info("[reachy] connected to daemon at %s:%s", self._host, self._port)

    def stop(self, timeout: float = 5.0) -> None:
        self._cancel_move()
        if self._play_thread and self._play_thread.is_alive():
            self._play_thread.join(timeout=timeout)
        with self._lock:
            if self._mini is None:
                return
            try:
                self._mini.goto_sleep()
            except Exception as e:
                logger.warning("[reachy] goto_sleep on stop failed: %s", e)
            # ReachyMini has no public close(); cleanup mirrors its __exit__.
            try:
                self._mini.client.disconnect()
            except Exception as e:
                logger.warning("[reachy] client.disconnect failed: %s", e)
            self._mini = None

    @property
    def is_connected(self) -> bool:
        return self._mini is not None

    # --- Animation / event dispatch ---

    def dispatch(self, event_type: str, payload: Any) -> None:
        from hal.presets import SERVO_CMD_PLAY

        if event_type == SERVO_CMD_PLAY and payload:
            self._play_recording(str(payload))
        else:
            # No client-side animation loop — idle/music grooves are a
            # feetech-backend concept; the daemon owns ambient behavior.
            logger.debug("[reachy] dispatch %s ignored", event_type)

    def get_available_recordings(self) -> List[str]:
        moves = self._recorded_moves()
        if moves is None:
            return []
        try:
            return sorted(moves.list_moves())
        except Exception as e:
            logger.warning("[reachy] list_moves failed: %s", e)
            return []

    def add_recording(self, name: str, actions: List[Dict[str, float]]) -> None:
        # CSV joint recordings are a feetech-backend concept; Reachy moves are
        # HF datasets played by the daemon. TODO(spike): translate CSV rows to
        # a Move if per-device uploaded animations turn out to matter here.
        logger.warning("[reachy] add_recording('%s') not supported on this backend", name)

    def ensure_running(self) -> None:
        # The daemon runs the control loop; nothing to restart client-side.
        pass

    @property
    def is_suppressed(self) -> bool:
        return self._suppressed

    # --- Freeze (camera stabilization) ---

    def freeze(self) -> None:
        self._frozen = True
        self._cancel_move()

    def unfreeze(self) -> None:
        self._frozen = False

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    # --- Motion primitives ---

    def move_to(self, target_positions: Dict[str, float], duration: float = 2.0) -> None:
        merged = {**self._current_or_target(), **target_positions}
        self._goto(merged, max(duration, _MIN_MOVE_DURATION_S))

    def move_and_hold(self, target_positions: Dict[str, float], duration: float = 2.0) -> None:
        # The daemon holds the last commanded pose by itself; identical to
        # move_to on this backend (no idle loop to fight).
        self.move_to(target_positions, duration)

    def get_joint_names(self) -> Set[str]:
        return set(JOINT_KEYS)

    def get_positions(self) -> Dict[str, float]:
        with self._lock:
            if self._mini is None:
                return dict(self._target)
            try:
                pose = self._mini.get_current_head_pose()  # 4x4, meters/rad
                xyz = np.asarray(pose)[:3, 3] * 1000.0
                rpy = Rotation.from_matrix(np.asarray(pose)[:3, :3]).as_euler(
                    "xyz", degrees=True
                )
                # head list order: body_rotation, stewart_1..6 (rad);
                # antenna order is [right, left] in the SDK.
                head_joints, ant = self._mini.get_current_joint_positions()
                return {
                    "head_x.pos": float(xyz[0]),
                    "head_y.pos": float(xyz[1]),
                    "head_z.pos": float(xyz[2]),
                    "head_roll.pos": float(rpy[0]),
                    "head_pitch.pos": float(rpy[1]),
                    "head_yaw.pos": float(rpy[2]),
                    "antenna_right.pos": math.degrees(float(ant[0])),
                    "antenna_left.pos": math.degrees(float(ant[1])),
                    _BODY_KEY: math.degrees(float(head_joints[0])),
                }
            except Exception as e:
                logger.warning("[reachy] get_positions read failed, using last target: %s", e)
                return dict(self._target)

    def send_positions(self, positions: Dict[str, float]) -> None:
        merged = {**self._current_or_target(), **positions}
        head, antennas, body_yaw = self._compose(merged)
        with self._lock:
            self._require_mini().set_target(head=head, antennas=antennas, body_yaw=body_yaw)
        self._target = merged

    # --- Postures & modes ---

    def zero_pose(self) -> None:
        self._cancel_move()
        self._goto({k: 0.0 for k in JOINT_KEYS}, 2.0)
        self._suppressed = True

    def release(self) -> Dict[str, str]:
        errors: Dict[str, str] = {}
        self._cancel_move()
        with self._lock:
            mini = self._require_mini()
            try:
                mini.goto_sleep()
            except Exception as e:
                errors["sleep"] = str(e)
            try:
                mini.disable_motors()
            except Exception as e:
                errors["torque"] = str(e)
        self._suppressed = True
        return errors

    def resume(self) -> None:
        with self._lock:
            mini = self._require_mini()
            try:
                mini.enable_motors()
            except Exception as e:
                logger.warning("[reachy] enable_motors failed: %s", e)
            try:
                mini.wake_up()
            except Exception as e:
                logger.warning("[reachy] wake_up failed: %s", e)
        self._suppressed = False

    def hold(self, explicit: bool = False) -> None:
        self._cancel_move()
        self._suppressed = True

    def joint_status(self) -> Dict[str, dict]:
        with self._lock:
            head, antennas = self._require_mini().get_current_joint_positions()  # rad
        status: Dict[str, dict] = {}
        # Names match the SDK motor ids (enable_motors/disable_motors vocabulary).
        for i, (name, angle) in enumerate(zip(_HEAD_MOTOR_IDS, head)):
            status[name] = {
                "online": True, "id": i + 1, "angle": round(math.degrees(float(angle)), 1),
            }
        for i, (name, angle) in enumerate(zip(("right_antenna", "left_antenna"), antennas)):
            status[name] = {
                "online": True, "id": len(_HEAD_MOTOR_IDS) + i + 1,
                "angle": round(math.degrees(float(angle)), 1),
            }
        return status

    # --- Aim & nudge ---

    def aim(self, direction: str, duration: float, current_positions: Dict[str, float],
            safety_policy: Any) -> Dict[str, float]:
        from hal.safety.policy import min_move_duration

        target = _AIM_TARGETS.get(direction)
        if target is None:
            raise ValueError(
                f"Unknown direction '{direction}'. Available: {list(_AIM_TARGETS.keys())}"
            )
        positions = {**current_positions, **target}
        eff = min_move_duration(safety_policy, positions, current_positions, duration)
        self._goto({**self._current_or_target(), **positions}, max(eff, _MIN_MOVE_DURATION_S))
        return positions

    def nudge(self, yaw: float, pitch: float, duration: float,
              current_positions: Dict[str, float],
              safety_policy: Any) -> Dict[str, float]:
        from hal.safety.policy import min_move_duration

        positions = dict(current_positions)
        if yaw != 0:
            positions["head_yaw.pos"] = current_positions.get("head_yaw.pos", 0.0) + yaw
        if pitch != 0:
            positions["head_pitch.pos"] = current_positions.get("head_pitch.pos", 0.0) + pitch

        eff = min_move_duration(safety_policy, positions, current_positions, duration)
        self._goto({**self._current_or_target(), **positions}, max(eff, _MIN_MOVE_DURATION_S))
        return positions

    # --- Internals ---

    def _require_mini(self) -> ReachyMini:
        if self._mini is None:
            raise RuntimeError("Reachy daemon not connected")
        return self._mini

    def _current_or_target(self) -> Dict[str, float]:
        """Best-effort current pose, falling back to the last commanded target."""
        try:
            return self.get_positions()
        except Exception:
            return dict(self._target)

    def _compose(self, positions: Dict[str, float]):
        """HAL joint dict (deg/mm) → SDK (4x4 head pose, antennas rad, body_yaw rad)."""
        head = create_head_pose(
            x=positions["head_x.pos"], y=positions["head_y.pos"], z=positions["head_z.pos"],
            roll=positions["head_roll.pos"], pitch=positions["head_pitch.pos"],
            yaw=positions["head_yaw.pos"],
            degrees=True, mm=True,
        )
        # SDK antenna order is [right, left].
        antennas = np.deg2rad(
            [positions["antenna_right.pos"], positions["antenna_left.pos"]]
        )
        body_yaw = math.radians(positions[_BODY_KEY])
        return head, antennas, body_yaw

    def _goto(self, positions: Dict[str, float], duration: float) -> None:
        head, antennas, body_yaw = self._compose(positions)
        with self._lock:
            self._require_mini().goto_target(
                head=head, antennas=antennas, body_yaw=body_yaw, duration=duration
            )
        self._target = dict(positions)

    def _cancel_move(self) -> None:
        with self._lock:
            if self._mini is None:
                return
            try:
                self._mini.cancel_move()
            except Exception as e:
                logger.debug("[reachy] cancel_move: %s", e)

    def _recorded_moves(self):
        """Lazy-load the HF emotes library. Returns the loader or None."""
        if self._moves is None:
            try:
                from reachy_mini.motion.recorded_move import RecordedMoves
                self._moves = RecordedMoves(_EMOTES_DATASET)
            except Exception as e:
                logger.warning("[reachy] recorded moves unavailable: %s", e)
                self._moves = False
        return self._moves or None

    def _play_recording(self, name: str) -> None:
        if self._suppressed or self._frozen:
            logger.debug("[reachy] play '%s' blocked (suppressed/frozen)", name)
            return
        if self._play_thread and self._play_thread.is_alive():
            self._cancel_move()

        def _run():
            moves = self._recorded_moves()
            if moves is None:
                logger.warning("[reachy] play '%s' skipped — no move library", name)
                return
            try:
                move = moves.get(name)
                self._current_recording = name
                with self._lock:
                    mini = self._require_mini()
                # play_move blocks for the move duration — keep it out of _lock
                # so state reads stay responsive; SDK serializes via the daemon.
                mini.play_move(move)
            except Exception as e:
                logger.warning("[reachy] play '%s' failed: %s", name, e)
            finally:
                self._current_recording = None

        self._play_thread = threading.Thread(
            target=_run, daemon=True, name="reachy-play"
        )
        self._play_thread.start()
