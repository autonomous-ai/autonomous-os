"""Silent-turn watchdog: a turn that is still working must not be killed.

A grounded turn produces no output until its search returns, which is
indistinguishable from a model that chose not to answer — unless you look at
whether the server is still sending anything at all.
"""

import threading
import time

from hal import config as app_config
from hal.realtime.models import OutputEvent, TextOutput
from hal.realtime.voice_agent.base import VoiceAgentBase


class _Agent(VoiceAgentBase):
    """Minimal concrete agent — receive() is all this exercises."""

    @property
    def sample_rate(self) -> int:
        return 16000

    def _do_connect(self): ...
    def _do_disconnect(self): ...
    def _send_loop(self): ...
    def _recv_loop(self): ...


def _fast_watchdog(monkeypatch, gap=0.15, cap=5.0):
    monkeypatch.setattr(app_config, "REALTIME_RECV_QUEUE_TIMEOUT_S", gap)
    monkeypatch.setattr(app_config, "REALTIME_TURN_MAX_SILENCE_S", cap)


def test_a_fully_silent_turn_still_ends(monkeypatch):
    _fast_watchdog(monkeypatch)
    agent = _Agent()
    assert list(agent.receive()) == []


def test_a_server_still_sending_keeps_the_turn_alive(monkeypatch):
    """No output for several gap windows, but messages keep arriving."""
    _fast_watchdog(monkeypatch)
    agent = _Agent()

    def busy():
        for _ in range(6):
            time.sleep(0.05)
            agent.note_server_activity()
        agent._recv_queue.put(OutputEvent(output=TextOutput(text="grounded answer")))

    threading.Thread(target=busy, daemon=True).start()
    assert [o.text for o in agent.receive()] == ["grounded answer"]


def test_the_cap_stops_a_server_that_never_produces_output(monkeypatch):
    _fast_watchdog(monkeypatch, gap=0.1, cap=0.4)
    agent = _Agent()
    stop = threading.Event()

    def chatter():
        while not stop.is_set():
            agent.note_server_activity()
            time.sleep(0.02)

    threading.Thread(target=chatter, daemon=True).start()
    started = time.monotonic()
    assert list(agent.receive()) == []
    stop.set()
    assert time.monotonic() - started < 3.0


def test_zero_cap_restores_the_plain_gap_watchdog(monkeypatch):
    _fast_watchdog(monkeypatch, gap=0.1, cap=0.0)
    agent = _Agent()
    agent.note_server_activity()
    assert list(agent.receive()) == []
