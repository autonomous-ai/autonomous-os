"""Short-lived follow-up focus for wake-word conversations."""

import threading
import time
from collections.abc import Callable


class WakeWordFocus:
    """Track a monotonic idle deadline shared by successive mic sessions."""

    def __init__(self, timeout_s: float, clock: Callable[[], float] = time.monotonic):
        self._timeout_s = max(0.0, timeout_s)
        self._clock = clock
        self._until = 0.0
        self._lock = threading.Lock()

    def is_active(self) -> bool:
        with self._lock:
            if self._until <= self._clock():
                self._until = 0.0
                return False
            return True

    def refresh(self, timeout_s: float | None = None) -> bool:
        """Extend focus from now; false when follow-up focus is disabled.

        `timeout_s` grants a SHORTER window than the default, for an opener
        that is inferred rather than deliberate. It never lengthens one: a
        wake word and a button click are explicit acts and keep the full
        window, while a gaze wake is a guess about intent and should not be
        able to claim more floor than the gestures it sits beside.
        """
        if self._timeout_s <= 0:
            return False
        window = self._timeout_s if timeout_s is None else min(self._timeout_s, max(0.0, timeout_s))
        if window <= 0:
            return False
        with self._lock:
            until = self._clock() + window
            # A later refresh must not SHORTEN a window already granted — a
            # deliberate wake mid-conversation would otherwise be cut back to a
            # gaze-sized one.
            self._until = max(self._until, until)
        return True
