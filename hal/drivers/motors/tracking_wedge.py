"""Backstop for a tracking lock left held with no owner.

`AnimationService._tracking_flag == True` with `_body_owners == 0`, sustained,
is an impossible state: the flag's only holder is a live tracking session, and
that session's ServoFollower holds a counter slot for as long as it writes the
bus. It happened anyway (#312) — an unlocked save/restore in
`aim.servo_ownership()` lost an update and re-asserted a stale True — and
nothing noticed, because the only code that clears the flag is the tracker's own
teardown. The device suppressed every emotion animation for twelve minutes and
recovered only when somebody walked in front of the camera.

The race itself is fixed at the source. This exists so that the next one, if
there is one, cannot outlive the condition that caused it.

Deliberately a separate module with no imports beyond stdlib: animation_service
pulls `lerobot`, a device-only dependency, so anything living there cannot be
tested on a development host.
"""

import time
from typing import Callable, Optional

# The real flag-without-writer window is the gap between tracker_service.py:367
# (flag set) and :394 (follower acquires its slot) — milliseconds. Two orders of
# magnitude of slack over that, and still 24x shorter than the observed wedge.
WEDGE_GRACE_S: float = 30.0


class TrackingWedgeWatchdog:
    """Decides when a held-but-unowned tracking flag has to be cleared.

    Stateful across calls (it times the condition) but otherwise pure: it never
    touches the animation service, so the caller keeps the decision to act and
    the log line that goes with it.
    """

    def __init__(
        self,
        grace_s: float = WEDGE_GRACE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._grace_s = grace_s
        self._clock = clock
        self._idle_since: Optional[float] = None

    def check(self, flag: bool, owners: int) -> Optional[float]:
        """Return how long the wedge has been held, or None if there is none.

        A non-None return is an instruction: clear the flag and say so. The
        timer resets on return, so one wedge produces one report rather than
        one per animation frame.
        """
        if not flag or owners > 0:
            self._idle_since = None
            return None

        now = self._clock()
        if self._idle_since is None:
            self._idle_since = now
            return None

        held = now - self._idle_since
        if held < self._grace_s:
            return None

        self._idle_since = None
        return held
