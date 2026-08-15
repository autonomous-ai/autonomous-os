"""Mock motion driver — a body made of variables.

The whole stack has needed a robot to run. This is the smallest thing that
satisfies `MotionService` without hardware: joints are floats in a dict, moves
land instantly, and every call is recorded so a test (or a person poking HAL on
a laptop) can see exactly what the robot was told to do.

It is used by `devices/sim` (the mock body) together with `HAL_BOARD=sim`. It
is not a simulator: nothing here models inertia, collision or time. It answers
the contract so routes, skills, markers and the safety gate can be exercised
end to end off-device.

    from hal.drivers.motors.mock_service import MockMotionService
    m = MockMotionService(); m.start()
    m.move_to({"base_yaw.pos": 20.0}, duration=0.5)
    m.calls[-1]        # ("move_to", {"base_yaw.pos": 20.0}, 0.5)
    m.get_positions()  # {"base_yaw.pos": 20.0, ...}
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("hal.motion.mock")

# The Lamp joint set, so skills and recordings written against the reference
# body work unchanged against the mock one.
DEFAULT_JOINTS = (
    "base_yaw.pos",
    "base_pitch.pos",
    "elbow_pitch.pos",
    "wrist_pitch.pos",
    "wrist_roll.pos",
)


class MockMotionService:
    """In-memory MotionService. Every mutation is recorded in `calls`."""

    def __init__(self, joints: Optional[Set[str]] = None) -> None:
        self._joints = set(joints or DEFAULT_JOINTS)
        self._positions: Dict[str, float] = {j: 0.0 for j in self._joints}
        self._lock = threading.Lock()
        self._connected = False
        self._suppressed = False
        self._frozen = False
        self._torque = True
        self._recordings: Dict[str, List[Dict[str, float]]] = {}
        self.calls: List[tuple] = []

    # --- Lifecycle ---

    def start(self) -> None:
        self._connected = True
        self._record("start")
        logger.info("[mock] motion up — %d joints, no hardware", len(self._joints))

    def stop(self, timeout: float = 5.0) -> None:
        self._connected = False
        self._record("stop", timeout)

    @property
    def is_connected(self) -> bool:
        return self._connected

    def ensure_running(self) -> None:
        self._connected = True

    # --- Animation / event dispatch ---

    def dispatch(self, event_type: str, payload: Any) -> None:
        self._record("dispatch", event_type, payload)

    def get_available_recordings(self) -> List[str]:
        return sorted(self._recordings)

    def add_recording(self, name: str, actions: List[Dict[str, float]]) -> None:
        self._recordings[name] = list(actions)
        self._record("add_recording", name, len(actions))

    # --- Freeze ---

    def freeze(self) -> None:
        self._frozen = True
        self._record("freeze")

    def unfreeze(self) -> None:
        self._frozen = False
        self._record("unfreeze")

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    @property
    def is_suppressed(self) -> bool:
        return self._suppressed

    # --- Motion primitives ---

    def move_to(self, target_positions: Dict[str, float], duration: float = 2.0) -> None:
        self._apply(target_positions)
        self._record("move_to", dict(target_positions), duration)

    def move_and_hold(self, target_positions: Dict[str, float], duration: float = 2.0) -> None:
        self._apply(target_positions)
        self._suppressed = True
        self._record("move_and_hold", dict(target_positions), duration)

    def get_joint_names(self) -> Set[str]:
        return set(self._joints)

    def get_positions(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._positions)

    def send_positions(self, positions: Dict[str, float]) -> None:
        self._apply(positions)
        self._record("send_positions", dict(positions))

    # --- Postures & modes ---

    def zero_pose(self) -> None:
        self._apply({j: 0.0 for j in self._joints})
        self._suppressed = True
        self._record("zero_pose")

    def release(self) -> Dict[str, str]:
        # Same shape as the real driver: travel to rest, then torque off. The
        # mock is where "release is not a stop" is easiest to see — the arm
        # moves first, and nothing here can abort a move in flight either.
        self._apply({j: 0.0 for j in self._joints})
        self._torque = False
        self._record("release")
        return {}

    def resume(self) -> None:
        self._torque = True
        self._suppressed = False
        self._record("resume")

    def hold(self, explicit: bool = False) -> None:
        self._suppressed = True
        self._record("hold", explicit)

    def joint_status(self) -> Dict[str, dict]:
        with self._lock:
            return {
                j: {"online": self._connected, "angle": self._positions[j], "id": i + 1}
                for i, j in enumerate(sorted(self._joints))
            }

    # --- Aim & nudge ---

    # Directions map to (yaw, pitch) degrees, mirroring the reference body's
    # convention: +yaw is left, +pitch is up.
    AIM_DIRECTIONS = {
        "center": (0.0, 0.0),
        "left": (30.0, 0.0),
        "right": (-30.0, 0.0),
        "up": (0.0, 25.0),
        "down": (0.0, -25.0),
    }

    def aim(self, direction: str, duration: float, current_positions: Dict[str, float],
            safety_policy: Any) -> Dict[str, float]:
        if direction not in self.AIM_DIRECTIONS:
            raise ValueError(f"unknown aim direction {direction!r} (known: {sorted(self.AIM_DIRECTIONS)})")
        yaw, pitch = self.AIM_DIRECTIONS[direction]
        target = {"base_yaw.pos": yaw, "base_pitch.pos": pitch}
        self._apply(target)
        self._record("aim", direction, duration)
        return self.get_positions()

    def nudge(self, yaw: float, pitch: float, duration: float,
              current_positions: Dict[str, float],
              safety_policy: Any) -> Dict[str, float]:
        base = current_positions or self.get_positions()
        target = {
            "base_yaw.pos": base.get("base_yaw.pos", 0.0) + yaw,
            "base_pitch.pos": base.get("base_pitch.pos", 0.0) + pitch,
        }
        self._apply(target)
        self._record("nudge", yaw, pitch, duration)
        return self.get_positions()

    # --- internals ---

    def _apply(self, positions: Dict[str, float]) -> None:
        with self._lock:
            for joint, value in positions.items():
                if joint in self._joints:
                    self._positions[joint] = float(value)

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, *args))
