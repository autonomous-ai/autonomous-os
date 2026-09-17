"""In-flight main-agent handoff state for the realtime layer (#419).

Pure bookkeeping, no I/O, no clock of its own: callers pass ``time.monotonic()``
so the guard is testable and cannot be skewed by NTP steps.

A handoff opens when the realtime model delegates a request to the main agent
(``RealtimeOrchestrator.save_main_handoff``) and closes when:

- the main agent's reply is fed back (``feed_realtime_history``), or
- the user cancels by click (``button_actions._cancel_agent_speech``), or
- ``ttl_s`` has passed with neither (a reply that never spoke — NO_REPLY, a
  hardware-only turn, a crashed backend — must not hold the device mute
  forever).

While open, the realtime entry points do not commit the user's audio to the
model; they speak a cached filler instead. The rate limit on that filler is
here too, so all three entry points share one clock.
"""

from __future__ import annotations

import threading


class MainHandoffTracker:
    def __init__(self, ttl_s: float, filler_gap_s: float) -> None:
        self._ttl_s: float = float(ttl_s)
        self._filler_gap_s: float = float(filler_gap_s)
        self._lock = threading.Lock()
        self._opened_at: float | None = None
        self._transcript: str = ""
        self._last_filler_at: float | None = None

    def open(self, transcript: str, now: float) -> None:
        """Record that ``transcript`` was handed to the main agent at ``now``.

        Re-opening replaces the previous handoff and restarts the TTL: a second
        delegation while the first is still open means the main agent has the
        newer request too (it merges same-session messages).
        """
        with self._lock:
            self._opened_at = now
            self._transcript = transcript
            self._last_filler_at = None

    def close(self, reason: str, now: float) -> bool:
        """Mark the handoff answered/abandoned. Returns whether one was open."""
        with self._lock:
            was_open = self._is_open_locked(now)
            self._opened_at = None
            self._transcript = ""
            self._last_filler_at = None
            return was_open

    def is_open(self, now: float) -> bool:
        with self._lock:
            return self._is_open_locked(now)

    def transcript(self) -> str:
        with self._lock:
            return self._transcript if self._opened_at is not None else ""

    def take_filler_slot(self, now: float) -> bool:
        """Claim the right to speak one filler. False while closed or inside
        ``filler_gap_s`` of the previous filler."""
        with self._lock:
            if not self._is_open_locked(now):
                return False
            last = self._last_filler_at
            if last is not None and now - last < self._filler_gap_s:
                return False
            self._last_filler_at = now
            return True

    def _is_open_locked(self, now: float) -> bool:
        if self._ttl_s <= 0 or self._opened_at is None:
            return False
        return now - self._opened_at < self._ttl_s
