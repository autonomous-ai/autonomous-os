"""Handing the body back to idle after a direct move parked it.

`AnimationService.move_and_hold` is the right call for pointing the head at
something — it preempts whatever recording is playing so the pose is not
overwritten a frame later — and it leaves the body with NOTHING playing:
`_current_recording = None`, `_idle_settled = True`, held "until the next
play/emotion/idle command". Nothing issues that command on its own. A caller
that parks the body and returns leaves the lamp motionless until an unrelated
emotion, `/servo/resume`, a gaze reacquire or a HAL restart happens to move it.

Measured three times, same signature each time (`[preempt] dropped recording
'idle' for a direct move`, then silence): lamp-0c89 03/09/2026 after a speech
reacquire (fixed in gaze), lamp-ac82 14/09/2026 after "Find my keyboard"
(`/servo/search` centred on the keyboard and stayed there), and the look-aim,
which has the same shape and is only less visible because gaze usually retakes
the body once a face is back in frame.

One helper for the three of them rather than a third copy of the gaze fix.
The guards are the ones gaze already had — tracking, hold mode and zero mode
each own the body and have their own release, and stealing it back from them is
the bug this exists to avoid repeating — plus one more: only a body that is
STILL parked is handed back. If an emotion started inside the window,
`_current_recording` is set and the animation loop already goes back to idle
when that recording ends.

The delayed form runs on a daemon timer so a caller that is inside a turn
(`search_for_subject` is awaited by curl from the agent's tool call) can return
its result immediately and still hand the body back later. One pending timer
at a time: a search started inside another search's window replaces the older
timer instead of stacking a second `play(idle)` behind it.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# How long the lamp keeps pointing at what it found before idle takes the body
# back. Long enough for the agent's reply to start playing over the pose; not
# tied to speech end because tracking has no speak-end hook to wait on.
HOLD_AFTER_FIND_S: float = 8.0

_pending: Optional[threading.Timer] = None
_pending_lock = threading.Lock()


def release_to_idle(reason: str) -> bool:
    """Dispatch play(idle) now, unless someone owns the body or something is
    already playing. Returns whether idle was dispatched. Never raises."""
    import hal.app_state as state

    svc = getattr(state, "animation_service", None)
    if svc is None:
        return False
    if getattr(svc, "_tracking_active", False):
        return False
    if getattr(svc, "_hold_mode", False) or getattr(svc, "_zero_mode", False):
        return False
    # Absent attribute (test doubles, older services) reads as parked: the
    # callers of this helper have all just parked the body themselves.
    if getattr(svc, "_current_recording", None) is not None:
        return False
    try:
        # Dispatch rather than _handle_play: playback belongs to the event
        # thread, the same handover the tracker performs when it ends.
        svc.dispatch("play", svc.idle_recording)
        logger.info("[body] %s — idle resumed", reason)
        return True
    except Exception as e:
        logger.warning("[body] could not resume idle after %s: %s", reason, e)
        return False


def cancel_pending_release() -> None:
    """Drop a scheduled handback, if any."""
    global _pending
    with _pending_lock:
        if _pending is not None:
            _pending.cancel()
            _pending = None


def release_to_idle_later(delay_s: float, reason: str) -> Optional[threading.Timer]:
    """Hand the body back after `delay_s`, replacing any earlier schedule.

    `delay_s <= 0` releases synchronously and returns None. Otherwise returns
    the daemon timer so a caller (or a test) can join it.
    """
    global _pending
    cancel_pending_release()
    if delay_s <= 0:
        release_to_idle(reason)
        return None

    def _fire() -> None:
        global _pending
        with _pending_lock:
            _pending = None
        release_to_idle(reason)

    t = threading.Timer(delay_s, _fire)
    t.daemon = True
    t.name = "body-release-to-idle"
    with _pending_lock:
        _pending = t
    t.start()
    return t
