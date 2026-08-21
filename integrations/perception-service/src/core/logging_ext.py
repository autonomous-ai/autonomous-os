"""A RotatingFileHandler that survives a failed write.

The stock handler never reopens a dead stream: once a write raises, that log is
silent until the process restarts. On 2026-08-17 lbserver.log stopped at 07:59:35
and stayed dead for the rest of the day while its mtime kept advancing -- writes
were still being attempted, still failing, and nothing ever tried again.

The trigger was storage-side (an EIO on a live, non-deleted file on MooseFS) and
cannot be prevented from here. What can be fixed is the permanence: reopen, and
if the file itself is unwritable, roll over to a fresh one.

This is filesystem-agnostic on purpose -- it covers a transient ENOSPC on local
disk just as well, which matters on a container whose root is 97% full.
"""

from __future__ import annotations

import logging
import sys
import time
from logging.handlers import RotatingFileHandler


class ResilientRotatingFileHandler(RotatingFileHandler):
    """Reopen on write failure; roll to a fresh file if the current one stays bad.

    Failure handling hangs off handleError() because logging.Handler.emit()
    already swallows exceptions and routes them there -- so super().emit() never
    raises and cannot be used to detect the fault.
    """

    def __init__(
        self,
        *args: object,
        retry_after: float = 30.0,
        failures_before_rollover: int = 3,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.retry_after = retry_after
        self.failures_before_rollover = failures_before_rollover
        self._consecutive_failures = 0
        self._muted_until = 0.0
        self._last_report = 0.0
        self._errored = False
        self._rolled_over_for_fault = False

    def emit(self, record: logging.LogRecord) -> None:
        # While muted, drop records rather than retry a known-broken stream for
        # every one. Retrying produced ~2 KB/s of tracebacks for 40 minutes in the
        # 2026-08-17 incident.
        if time.monotonic() < self._muted_until:
            return
        self._errored = False
        super().emit(record)
        # Only a write that did NOT route through handleError counts as recovery.
        # Checking `self.stream is not None` instead would clear the counter after
        # every reopen, so the rollover threshold would never be reached.
        if not self._errored and self._consecutive_failures:
            self._consecutive_failures = 0
            self._rolled_over_for_fault = False

    def handleError(self, record: logging.LogRecord) -> None:
        """Called by Handler.emit() when the write raised. Try to get writable again."""
        self._errored = True
        self._consecutive_failures += 1
        self._report(record)
        try:
            self._close_stream()
            if (
                self._consecutive_failures >= self.failures_before_rollover
                and not self._rolled_over_for_fault
            ):
                # Reopening the same path keeps failing if the file itself is the
                # problem, so take a fresh inode -- but only once per fault, or a
                # persistent fault would churn through every backup and destroy
                # the history we are trying to keep.
                self.doRollover()
                self._rolled_over_for_fault = True
            else:
                self.stream = self._open()
                if self._consecutive_failures >= self.failures_before_rollover:
                    # A fresh file did not help either; stop trying for a while.
                    self._muted_until = time.monotonic() + self.retry_after
        except Exception:
            # Recovery itself failed: back off so we neither spin nor flood.
            self._close_stream()
            self._muted_until = time.monotonic() + self.retry_after

    def _close_stream(self) -> None:
        if self.stream is not None:
            try:
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def _report(self, record: logging.LogRecord) -> None:
        """Rate-limited failure notice. The stock handler prints a ~40 line
        traceback per record, which is the flood this class exists to avoid."""
        if not logging.raiseExceptions or not sys.stderr:
            return
        now = time.monotonic()
        if now - self._last_report < self.retry_after:
            return
        self._last_report = now
        exc = sys.exc_info()[1]
        try:
            sys.stderr.write(
                f"[logging] write failed on {self.baseFilename}: {exc!r} "
                f"(failure {self._consecutive_failures}; further notices "
                f"suppressed for {self.retry_after:.0f}s)\n"
            )
        except Exception:
            pass
