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


def is_addressed(
    wakeword_enabled: bool,
    wake_word_heard: bool,
    focus_latched_at_session_start: bool,
    focus_active_now: bool,
) -> bool:
    """Whether the sentence being spoken has been shown to be for this device.

    Asked by everything that claims to be the addressee — the listening cue,
    the backchannel — so the device does not acknowledge a conversation it was
    never part of.

    ``focus_active_now`` is read LIVE, and that is the point: gaze can open the
    follow-up window in the MIDDLE of the sentence it is meant to acknowledge.
    Device-observed 04/09/2026 on lamp-0c89 — at speech start the camera had no
    face evidence ("of 0" samples) so the session-start latch was False, and the
    watcher only confirmed the user 3.6s later at speech END. The whole turn ran
    with no listening cue: the device sat dark through the sentence and lit up
    only for the next one.

    Live focus can only ADD an addressed turn, never remove one — the latch is
    still passed in and still wins, so a window that EXPIRES mid-sentence cannot
    retract a turn from someone already speaking.
    """
    if not wakeword_enabled:
        return True
    return wake_word_heard or focus_latched_at_session_start or focus_active_now
