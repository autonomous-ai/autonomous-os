"""Motion drivers.

`MotorsService` is imported lazily (PEP 562): it pulls hal.follower →
lerobot → torch, which only installs on a robot. Importing this package must
stay cheap so the mock body (devices/sim, driver: mock) can run HAL on a
laptop with none of that present.
"""

__all__ = ["MotorsService"]


def __getattr__(name):
    if name == "MotorsService":
        from .motors_service import MotorsService

        return MotorsService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
