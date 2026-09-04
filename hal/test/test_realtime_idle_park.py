"""The device must not let the server close an idle Gemini session.

An idle session is killed upstream with WS 1008 "The operation was aborted";
the backend logs that as an error and alerts its dev channel. The idle watchdog
closes the transport first, and the next turn reconnects on demand.
"""

import threading
import time
from types import SimpleNamespace

from hal import config as hal_config
from hal.realtime.orchestrator import RealtimeOrchestrator


class _Agent:
    def __init__(self, available: bool = True):
        self.available = available
        self.disconnected = 0
        self.requires_fresh_session = False

    def disconnect(self):
        self.disconnected += 1
        self.available = False


def _orch(monkeypatch, *, idle_s: float, threshold: float = 45.0, agent=None):
    monkeypatch.setattr(hal_config, "REALTIME_PROVIDER", "gemini", raising=False)
    monkeypatch.setattr(
        hal_config, "REALTIME_GEMINI_IDLE_PARK_S", threshold, raising=False
    )
    o = object.__new__(RealtimeOrchestrator)
    o._started = threading.Event()
    o._started.set()
    o._rebuild_lock = threading.Lock()
    o._rebuild_done = threading.Event()
    o._idle_parked = False
    o._park_resume_failed = False
    o._turn_in_flight = False
    o._turn_started_monotonic = 0.0
    o._agent = _Agent() if agent is None else agent
    o._last_activity_monotonic = time.monotonic() - idle_s
    o._last_turn_monotonic = 0.0
    return o


def test_parks_after_threshold(monkeypatch):
    o = _orch(monkeypatch, idle_s=90)
    agent = o._agent
    o._maybe_park_idle_session()
    assert agent.disconnected == 1, "an idle session must be closed by the device"
    assert o._idle_parked is True


def test_no_park_below_threshold(monkeypatch):
    o = _orch(monkeypatch, idle_s=10)
    o._maybe_park_idle_session()
    assert o._agent.disconnected == 0
    assert o._idle_parked is False


def test_threshold_zero_disables(monkeypatch):
    o = _orch(monkeypatch, idle_s=900, threshold=0.0)
    o._maybe_park_idle_session()
    assert o._agent.disconnected == 0


def test_non_gemini_provider_untouched(monkeypatch):
    o = _orch(monkeypatch, idle_s=900)
    monkeypatch.setattr(hal_config, "REALTIME_PROVIDER", "openai", raising=False)
    o._maybe_park_idle_session()
    assert o._agent.disconnected == 0


def test_turn_in_flight_blocks_park(monkeypatch):
    """Parking mid-turn would kill the turn the user is waiting on."""
    o = _orch(monkeypatch, idle_s=90)
    o._turn_in_flight = True
    o._turn_started_monotonic = time.monotonic()
    o._maybe_park_idle_session()
    assert o._agent.disconnected == 0


def test_abandoned_turn_marker_expires(monkeypatch):
    """A prepared-then-abandoned turn must not disable parking forever."""
    o = _orch(monkeypatch, idle_s=900)
    o._turn_in_flight = True
    o._turn_started_monotonic = time.monotonic() - 600
    o._maybe_park_idle_session()
    assert o._agent.disconnected == 1


def test_parked_session_still_available(monkeypatch):
    """Reporting unavailable while parked would route every post-idle turn to
    the main agent — worse than the 1008 this replaces."""
    o = _orch(monkeypatch, idle_s=90)
    o._maybe_park_idle_session()
    assert o.available is True


def test_prepare_turn_resumes_parked_session(monkeypatch):
    o = _orch(monkeypatch, idle_s=90)
    o._maybe_park_idle_session()
    rebuilt = []
    o._rebuild_now = lambda reason, **kw: (rebuilt.append(reason), True)[1]
    o._skip_post_idle_recycle = False
    o.prepare_turn()
    assert rebuilt == ["idle-park-resume"]
    assert o._skip_post_idle_recycle is True


def test_failed_resume_reports_unavailable(monkeypatch):
    """A resume that cannot connect must fall the turn back to the main agent
    rather than let it stream into the closed transport."""
    o = _orch(monkeypatch, idle_s=90)
    o._maybe_park_idle_session()
    o._rebuild_now = lambda reason, **kw: False
    o._skip_post_idle_recycle = False
    o.prepare_turn()
    assert o.available is False
    assert o._idle_parked is True, "stay parked so the next turn retries the resume"
