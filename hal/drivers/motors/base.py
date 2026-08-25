"""Motion service contract — the interface every motion driver must satisfy.

A typing.Protocol (duck-typed, no inheritance required). AnimationService
(feetech/lerobot) satisfies it today; future drivers (e.g. ReachyMotionService
wrapping Pollen's SDK) implement the same surface so routes/servo.py works
unchanged.

Design notes:
  - No ABC, no lerobot/torch import — dependency-free.
  - `move_to_raw` is deliberately omitted: it writes raw STS3215 register
    values and is feetech-specific. Drivers that need direct register access
    keep it as an internal method.
  - `dispatch` / `get_available_recordings` / `add_recording` may no-op on
    backends that are not animation-based (e.g. a robot SDK that exposes
    move_to directly).
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Set, runtime_checkable

from typing_extensions import Protocol


@runtime_checkable
class MotionService(Protocol):
    """The contract that routes/servo.py and the rest of HAL talk to."""

    # --- Lifecycle ---

    # skip_wake: bring the body up WITHOUT its startup pose / idle loop, for a
    # HAL restart on a device that was asleep. Optional so a driver with no
    # wake sequence can ignore it.
    def start(self, skip_wake: bool = False) -> None: ...
    def stop(self, timeout: float = 5.0) -> None: ...

    @property
    def is_connected(self) -> bool: ...

    # --- Animation / event dispatch ---

    def dispatch(self, event_type: str, payload: Any) -> None: ...
    def get_available_recordings(self) -> List[str]: ...
    def add_recording(self, name: str, actions: List[Dict[str, float]]) -> None: ...
    def ensure_running(self) -> None:
        """Restart the event loop if it stopped (e.g. after zero/hold)."""
        ...

    @property
    def is_suppressed(self) -> bool:
        """True when zero_pose or hold is active — idle/ambient animations suppressed."""
        ...

    # --- Freeze (camera stabilization) ---

    def freeze(self) -> None: ...
    def unfreeze(self) -> None: ...

    @property
    def is_frozen(self) -> bool: ...

    # --- Motion primitives ---

    def move_to(self, target_positions: Dict[str, float], duration: float = 2.0) -> None: ...
    def move_and_hold(self, target_positions: Dict[str, float], duration: float = 2.0) -> None: ...

    def get_joint_names(self) -> Set[str]:
        """Return the set of valid joint keys, e.g. {"base_yaw.pos", "base_pitch.pos", ...}."""
        ...

    def get_positions(self) -> Dict[str, float]:
        """Read current joint positions (hardware). Keys match get_joint_names()."""
        ...

    def send_positions(self, positions: Dict[str, float]) -> None:
        """Write joint positions directly (one-shot, no interpolation)."""
        ...

    # --- Postures & modes ---

    def zero_pose(self) -> None:
        """Move to the device's zero/park pose and hold (torque stays ON)."""
        ...

    def release(self) -> Dict[str, str]:
        """Move to gravity-rest, then disable torque. Returns per-motor error dict (empty=ok)."""
        ...

    def halt(self) -> None:
        """Abort any move, recording or tracking in flight and HOLD where the body is.

        This is the deterministic stop. It is NOT `release`: torque stays ON and
        the body does not travel to a rest pose first — a stop that moves is
        wrong for anything with legs or wheels. It is NOT `stop` either, which
        is the service lifecycle (see above).

        Must be safe to call at any time, including when nothing is moving, and
        must never be gated by a safety bound (`motion.stop_always`).
        """
        ...

    def resume(self) -> None:
        """Exit zero/hold, re-enable torque, restart idle animation."""
        ...

    def hold(self, explicit: bool = False) -> None:
        """Suppress idle animations but keep torque ON.
        explicit=True also suppresses scene-change emotions."""
        ...

    def joint_status(self) -> Dict[str, dict]:
        """Per-joint online/offline status with angle and servo ID."""
        ...

    # --- Aim & nudge (device-specific joint mapping lives inside the driver) ---

    def aim(self, direction: str, duration: float, current_positions: Dict[str, float],
            safety_policy: Any) -> Dict[str, float]:
        """Aim to a named direction. Returns the final joint positions dict."""
        ...

    def nudge(self, yaw: float, pitch: float, duration: float,
              current_positions: Dict[str, float],
              safety_policy: Any) -> Dict[str, float]:
        """Relative nudge from current position. Returns the final joint positions dict."""
        ...
