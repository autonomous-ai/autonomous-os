"""The in-flight main handoff guard (#419).

While the main agent is working on a delegated request, the realtime layer
must not answer a nudge from memory. The tracker is the deterministic state
behind that: opened on delegation, closed when the reply lands / the user
clicks / the TTL passes.
"""

from hal.realtime.main_handoff import MainHandoffTracker
from hal.realtime.orchestrator import RealtimeOrchestrator


def _tracker(ttl_s: float = 120.0, gap_s: float = 4.0) -> MainHandoffTracker:
    return MainHandoffTracker(ttl_s=ttl_s, filler_gap_s=gap_s)


def test_closed_by_default():
    t = _tracker()
    assert not t.is_open(now=10.0)
    assert t.transcript() == ""


def test_open_then_reply_closes():
    t = _tracker()
    t.open("Find my pen", now=10.0)
    assert t.is_open(now=30.0)
    assert t.transcript() == "Find my pen"

    assert t.close("main_reply", now=31.0) is True
    assert not t.is_open(now=31.0)
    assert t.transcript() == ""


def test_close_when_already_closed_reports_false():
    t = _tracker()
    assert t.close("click", now=1.0) is False


def test_ttl_expires_the_handoff():
    t = _tracker(ttl_s=120.0)
    t.open("Find my pen", now=10.0)
    assert t.is_open(now=129.9)
    assert not t.is_open(now=130.0)


def test_ttl_zero_disables_the_guard():
    t = _tracker(ttl_s=0)
    t.open("Find my pen", now=10.0)
    assert not t.is_open(now=10.0)


def test_reopen_restarts_the_ttl():
    t = _tracker(ttl_s=60.0)
    t.open("first", now=0.0)
    t.open("second", now=50.0)
    assert t.is_open(now=100.0)
    assert t.transcript() == "second"


def test_filler_slot_is_rate_limited():
    t = _tracker(gap_s=4.0)
    t.open("Find my pen", now=0.0)
    assert t.take_filler_slot(now=1.0) is True
    assert t.take_filler_slot(now=3.0) is False
    assert t.take_filler_slot(now=5.0) is True


def test_filler_slot_never_granted_while_closed():
    t = _tracker()
    assert t.take_filler_slot(now=1.0) is False


class _RecordingContext:
    def __init__(self) -> None:
        self.turns: list[tuple[str, str]] = []

    def add_turn(self, user_text: str, agent_text: str) -> None:
        self.turns.append((user_text, agent_text))


def _orchestrator(ttl_s: float = 120.0) -> RealtimeOrchestrator:
    orchestrator = object.__new__(RealtimeOrchestrator)
    orchestrator._context = _RecordingContext()
    orchestrator._main_handoff = MainHandoffTracker(ttl_s=ttl_s, filler_gap_s=4.0)
    return orchestrator


def test_delegation_opens_the_handoff():
    o = _orchestrator()
    assert not o.main_handoff_open()

    o.save_main_handoff("Find my pen")

    assert o.main_handoff_open()
    assert o.main_handoff_transcript() == "Find my pen"


def test_main_agent_reply_closes_the_handoff():
    o = _orchestrator()
    o.save_main_handoff("Find my pen")

    o.save_main_agent_reply_fragment("Found it, about fifty degrees to your right.")

    assert not o.main_handoff_open()


def test_blank_reply_fragment_does_not_close():
    o = _orchestrator()
    o.save_main_handoff("Find my pen")

    o.save_main_agent_reply_fragment("   ")

    assert o.main_handoff_open()


def test_close_main_handoff_reports_whether_one_was_open():
    o = _orchestrator()
    assert o.close_main_handoff("click") is False
    o.save_main_handoff("Find my pen")
    assert o.close_main_handoff("click") is True
    assert not o.main_handoff_open()


def test_filler_slot_goes_through_the_tracker():
    o = _orchestrator()
    o.save_main_handoff("Find my pen")
    assert o.take_main_handoff_filler_slot() is True
    assert o.take_main_handoff_filler_slot() is False  # inside the 4 s gap
