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

A handoff carries the os-server run id of the turn it is waiting on, bound
once dispatch returns it. Closing is then id-matched: a reply from an OLDER
run must not release a handoff opened for a NEWER one. Without that check, the
sequence "ask A → click → ask B → A's reply finally lands" closed B's handoff
and put the #419 hole straight back, and a long tool (`/servo/search`) holds
that window open for tens of seconds.
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
        self._run_id: str = ""
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
            self._run_id = ""
            self._last_filler_at = None

    def bind_run(self, run_id: str) -> None:
        """Attach the os-server run id of the turn this handoff is waiting on.

        Dispatch mints the id, so it arrives a moment after ``open``. Until it
        does the handoff is unbound and any reply may close it — a window of
        milliseconds, and the alternative (refusing every reply) would strand
        the guard for the whole TTL whenever dispatch returns no id at all.
        """
        with self._lock:
            if self._opened_at is not None:
                self._run_id = run_id or ""

    def run_id(self) -> str:
        with self._lock:
            return self._run_id if self._opened_at is not None else ""

    def close(self, reason: str, now: float, run_id: str | None = None) -> bool:
        """Mark the handoff answered/abandoned. Returns whether one was closed.

        ``run_id`` is the run the closing reply belongs to:

        - ``None`` — close unconditionally. The physical click means "drop what
          you are doing", not "drop run X".
        - a run id — close only when it matches the handoff, so a late reply
          from a superseded turn cannot release a newer one.
        - ``""`` — the caller has no id (an os-server that predates this
          field). Treated as a match rather than refused: the old, occasionally
          too-eager close is a better failure than a guard stuck for the TTL.
        """
        with self._lock:
            if not self._is_open_locked(now):
                return False
            if run_id and self._run_id and run_id != self._run_id:
                return False
            self._opened_at = None
            self._transcript = ""
            self._run_id = ""
            self._last_filler_at = None
            return True

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
