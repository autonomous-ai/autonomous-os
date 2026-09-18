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

    o.begin_main_handoff("Find my pen")

    assert o.main_handoff_open()
    assert o.main_handoff_transcript() == "Find my pen"


def test_memory_write_alone_does_not_arm_the_guard():
    """save_main_handoff runs for every turn reaching the main agent, including
    the no-output fallback whose "request" is often a noise fragment. Arming
    there made 'you.' stonewall the next real request (green-lamp, 18/9)."""
    o = _orchestrator()

    o.save_main_handoff("you.")

    assert not o.main_handoff_open()
    assert o._context.turns == [(
        "you.",
        "[This request was handed to the main agent; its spoken reply follows.]",
    )]


def test_main_agent_reply_closes_the_handoff():
    o = _orchestrator()
    o.begin_main_handoff("Find my pen")

    o.save_main_agent_reply_fragment("Found it, about fifty degrees to your right.")

    assert not o.main_handoff_open()


def test_blank_reply_fragment_does_not_close():
    o = _orchestrator()
    o.begin_main_handoff("Find my pen")

    o.save_main_agent_reply_fragment("   ")

    assert o.main_handoff_open()


def test_close_main_handoff_reports_whether_one_was_open():
    o = _orchestrator()
    assert o.close_main_handoff("click") is False
    o.begin_main_handoff("Find my pen")
    assert o.close_main_handoff("click") is True
    assert not o.main_handoff_open()


def test_filler_slot_goes_through_the_tracker():
    o = _orchestrator()
    o.begin_main_handoff("Find my pen")
    assert o.take_main_handoff_filler_slot() is True
    assert o.take_main_handoff_filler_slot() is False  # inside the 4 s gap


# --- id-matched closing -------------------------------------------------
# A reply only releases the handoff it belongs to. Without this, "ask A →
# click → ask B → A's reply finally lands" closed B's handoff and put the
# #419 hole straight back for B.


def test_a_reply_from_another_run_does_not_close_the_handoff():
    t = _tracker()
    t.open("Find my pen", now=0.0)
    t.bind_run("device-chat-7-1787885628360")

    assert t.close("main_reply", now=1.0, run_id="device-chat-6-1787885620000") is False
    assert t.is_open(now=1.0)
    assert t.close("main_reply", now=1.0, run_id="device-chat-7-1787885628360") is True


def test_an_unbound_handoff_accepts_any_reply():
    """Dispatch may return no run id at all; refusing every reply would strand
    the guard for the whole TTL."""
    t = _tracker()
    t.open("Find my pen", now=0.0)

    assert t.close("main_reply", now=1.0, run_id="device-chat-9-1787885629999") is True


def test_the_click_closes_whatever_is_open():
    t = _tracker()
    t.open("Find my pen", now=0.0)
    t.bind_run("device-chat-7-1787885628360")

    assert t.close("click", now=1.0) is True


def test_binding_is_ignored_once_the_handoff_is_closed():
    t = _tracker()
    t.open("Find my pen", now=0.0)
    t.close("click", now=1.0)
    t.bind_run("device-chat-7-1787885628360")

    assert t.run_id() == ""


def test_the_reported_regression_sequence(monkeypatch):
    """ask A → click → ask B → A's late reply must not release B."""
    o = _orchestrator()
    o.begin_main_handoff("Find my pen")
    o.bind_main_handoff_run("run-A")

    o.close_main_handoff("click")            # user clicks: A is dropped
    o.begin_main_handoff("Turn off the TV")  # B delegated
    o.bind_main_handoff_run("run-B")

    o.save_main_agent_reply_fragment("Found it at fifty-one degrees.", run_id="run-A")

    assert o.main_handoff_open(), "A's late reply must not release B's handoff"
    assert o.main_handoff_transcript() == "Turn off the TV"

    o.save_main_agent_reply_fragment("The TV is off.", run_id="run-B")
    assert not o.main_handoff_open()


def test_a_reply_without_a_run_id_still_closes():
    """An os-server that predates the field must not strand the guard."""
    o = _orchestrator()
    o.begin_main_handoff("Find my pen")
    o.bind_main_handoff_run("run-A")

    o.save_main_agent_reply_fragment("Found it.")

    assert not o.main_handoff_open()


def test_a_reply_with_no_handoff_open_is_not_reported_as_a_refusal(caplog):
    """The ordinary case — every main-agent reply when nothing is pending —
    must stay silent in the log, or real refusals drown in it."""
    import logging

    o = _orchestrator()
    with caplog.at_level(logging.INFO, logger="hal.realtime"):
        assert o.close_main_handoff("main_reply", run_id="run-A") is False
    assert "kept open" not in caplog.text


def test_a_genuine_refusal_is_logged(caplog):
    import logging

    o = _orchestrator()
    o.begin_main_handoff("Find my pen")
    o.bind_main_handoff_run("run-B")
    with caplog.at_level(logging.INFO, logger="hal.realtime"):
        assert o.close_main_handoff("main_reply", run_id="run-A") is False
    assert "kept open" in caplog.text


def test_lifecycle_end_releases_a_handoff_whose_reply_never_spoke():
    """The reply feed is not a reliable release: on green-lamp (18/9) the
    agent's reply was dropped as a CoT leak, so nothing closed the handoff and
    the next real utterance was stonewalled. lifecycle_end is the backstop."""
    o = _orchestrator()
    o.begin_main_handoff("Find my pen")
    o.bind_main_handoff_run("run-A")

    assert o.close_main_handoff("lifecycle_end", run_id="run-A") is True
    assert not o.main_handoff_open()


def test_lifecycle_end_of_another_run_leaves_the_handoff_alone():
    o = _orchestrator()
    o.begin_main_handoff("Find my pen")
    o.bind_main_handoff_run("run-A")

    assert o.close_main_handoff("lifecycle_end", run_id="run-B") is False
    assert o.main_handoff_open()
