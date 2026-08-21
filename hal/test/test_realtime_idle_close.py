"""Gemini closes a session it receives nothing on (WS 1008). HAL holds sessions
open between turns, so an idle device hands the provider something to kill. The
watchdog hangs up first, turning an upstream error into a local disconnect."""

import threading
import time
from types import SimpleNamespace

from hal import config as hal_config
from hal.realtime.orchestrator import RealtimeOrchestrator


class _Agent:
    def __init__(self) -> None:
        self.disconnected = False

    def disconnect(self) -> None:
        self.disconnected = True


def _orch(monkeypatch, idle_s: float, threshold: float = 60.0, provider: str = "gemini"):
    monkeypatch.setattr(hal_config, "REALTIME_PROVIDER", provider, raising=False)
    monkeypatch.setattr(
        hal_config, "REALTIME_GEMINI_PRE_TURN_RECYCLE_S", threshold, raising=False
    )
    o = object.__new__(RealtimeOrchestrator)
    o._agent = _Agent()
    o._started = threading.Event(); o._started.set()
    o._idle_close_stop = threading.Event()
    o._lifecycle_lock = threading.RLock()
    o._last_turn_monotonic = time.monotonic() - idle_s
    o._rebuild_lock = threading.Lock()   # `rebuilding` reads .locked(), not a bool
    return o


def _tick(o) -> None:
    """Run exactly one watchdog iteration.

    The loop is `while not stop.is_set(): if stop.wait(...): return`. So the stop
    event must stay UNSET (or the body never runs at all), and wait() has to
    return False once — letting one pass through — then True to end the loop.
    """
    calls = {"n": 0}
    original = o._idle_close_stop.wait

    def wait(_timeout=None):
        calls["n"] += 1
        return calls["n"] > 1
    o._idle_close_stop.wait = wait     # type: ignore[method-assign]
    try:
        o._idle_close_loop()
    finally:
        o._idle_close_stop.wait = original  # type: ignore[method-assign]


def test_closes_a_session_idle_past_the_threshold(monkeypatch):
    o = _orch(monkeypatch, idle_s=90)
    agent = o._agent
    _tick(o)
    assert agent.disconnected, "an idle session must be hung up before the provider kills it"
    assert o._agent is None


def test_keeps_a_session_that_is_still_in_use(monkeypatch):
    """An active conversation must never lose its session to the watchdog."""
    o = _orch(monkeypatch, idle_s=10)
    agent = o._agent
    _tick(o)
    assert not agent.disconnected
    assert o._agent is agent


def test_leaves_a_never_used_preconnect_alone(monkeypatch):
    """_last_turn_monotonic == 0 means no turn has happened yet. Closing that
    session would defeat the pre-connect it exists to be."""
    o = _orch(monkeypatch, idle_s=999)
    o._last_turn_monotonic = 0.0
    agent = o._agent
    _tick(o)
    assert not agent.disconnected


def test_threshold_zero_disables(monkeypatch):
    o = _orch(monkeypatch, idle_s=999, threshold=0)
    agent = o._agent
    _tick(o)
    assert not agent.disconnected


def test_non_gemini_provider_untouched(monkeypatch):
    o = _orch(monkeypatch, idle_s=999, provider="openai")
    agent = o._agent
    _tick(o)
    assert not agent.disconnected


def test_does_not_fire_twice_while_still_idle(monkeypatch):
    """After hanging up, the clock is cleared so the watchdog goes quiet instead
    of logging every poll until someone speaks."""
    o = _orch(monkeypatch, idle_s=90)
    _tick(o)
    assert o._last_turn_monotonic == 0.0
    o._agent = _Agent()
    _tick(o)
    assert not o._agent.disconnected


# The whole cost argument rests on these two being the same number. If they drift,
# turns landing between them start paying handshakes they did not pay before.
def test_close_threshold_is_tied_to_the_recycle_threshold(monkeypatch):
    monkeypatch.setattr(hal_config, "REALTIME_GEMINI_PRE_TURN_RECYCLE_S", 137.0, raising=False)
    o = object.__new__(RealtimeOrchestrator)
    assert o._idle_close_threshold() == 137.0
