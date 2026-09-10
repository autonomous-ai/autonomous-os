"""The AnimationService body-ownership contract, for test doubles.

Mirrors hal/drivers/motors/animation_service.py:187-254. Four test files used
to carry a plain `_tracking_active = bool` attribute instead, which cannot
express refcounted ownership: once `aim.servo_ownership()` started calling
`acquire_body()`, those fakes raised AttributeError inside the context
manager's `except Exception` guard, so ownership was silently never claimed
and the assertions passed against a lock nobody held.

Class-level defaults rather than an `__init__`, so a fake can mix this in
without disturbing its own constructor chain (the gaze doubles take positional
arguments and call `super().__init__(wrist)`). `self._body_owners += 1` reads
the class default and writes an instance attribute, so each double still counts
its own owners.

The lock is shared by every double rather than held per instance, for the same
reason. Tests contend for it approximately never, and a shared lock is the
conservative direction: it can only serialise more than the real one does.
"""

import threading


class BodyOwnership:
    _tracking_flag: bool = False
    _body_owners: int = 0
    _body_owner_lock = threading.Lock()

    @property
    def _tracking_active(self) -> bool:
        """True while anything owns the body — a flag holder or a live writer."""
        return self._tracking_flag or self._body_owners > 0

    @_tracking_active.setter
    def _tracking_active(self, value: bool) -> None:
        # Assignment sets the FLAG only, exactly as the real service does. It
        # cannot release a writer that is still running.
        self._tracking_flag = bool(value)

    def acquire_body(self) -> None:
        with self._body_owner_lock:
            self._body_owners += 1

    def release_body(self) -> None:
        with self._body_owner_lock:
            self._body_owners = max(0, self._body_owners - 1)
