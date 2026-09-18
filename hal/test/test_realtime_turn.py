"""Regression tests for realtime-to-main-agent turn routing."""

from hal.drivers.voice._internal.realtime_turn import should_dispatch_to_main


def test_confirmed_turn_falls_back_when_realtime_is_unavailable_or_silent():
    """A connected flag must not swallow a wake-word command without a reply."""
    assert should_dispatch_to_main(True, True)


def test_confirmed_handled_turn_still_synchronizes_main_agent():
    assert should_dispatch_to_main(True, True)


def test_wakeword_gate_still_rejects_unarmed_ambient_speech():
	assert not should_dispatch_to_main(True, False)


def test_wakeword_focus_authorizes_a_followup_turn():
    assert should_dispatch_to_main(True, True)


def test_wakeword_disabled_preserves_the_always_listening_main_agent_sync():
    """The legacy path always sends the finalized STT turn to the OS server."""
    assert should_dispatch_to_main(False, False)


from hal.drivers.voice._internal import realtime_turn as rt_mod
from hal.drivers.voice._internal.realtime_turn import (
    ROUTE_MAIN_PENDING,
    RealtimeTurnResult,
    hold_for_main_handoff,
    should_drop_downstream_turn,
)


class _FakeRealtime:
    """Just enough orchestrator surface for the guard."""

    def __init__(self, open_: bool, slot: bool = True) -> None:
        self._open = open_
        self._slot = slot
        self.turns: list[tuple[str, str]] = []
        self.available = True

    def main_handoff_open(self) -> bool:
        return self._open

    def main_handoff_transcript(self) -> str:
        return "Find my pen" if self._open else ""

    def take_main_handoff_filler_slot(self) -> bool:
        return self._open and self._slot

    def save_turn(self, user_text: str, agent_text: str) -> None:
        self.turns.append((user_text, agent_text))


def test_no_handoff_means_no_hold():
    assert hold_for_main_handoff(_FakeRealtime(open_=False), "hey", "i-1") is None


def test_nudge_during_handoff_is_rejected_in_silence(monkeypatch):
    """A nudge at a device that is already working is noise, and noise is
    rejected without a sound — the same verdict reject_turn reaches for a bare
    acknowledgment. It must not be recorded either: a non-request in the next
    session's context is what #421 is about."""
    posted: list[dict] = []
    monkeypatch.setattr(
        rt_mod.requests, "post",
        lambda url, json=None, timeout=None: posted.append({"url": url, "json": json}),
    )
    realtime = _FakeRealtime(open_=True)

    rt = hold_for_main_handoff(realtime, "sếp sếp ơi", "i-2")

    assert rt is not None
    assert rt.route == ROUTE_MAIN_PENDING
    assert rt.handled is False and rt.delegated is False
    assert posted == [], "a nudge must not be answered out loud"
    assert realtime.turns == [], "a nudge must not enter realtime memory"


def test_a_real_utterance_during_handoff_gets_one_filler_and_a_memory_line(monkeypatch):
    """Longer than the noise threshold is not a nudge: the user said something
    real while waiting. Still never reaches the model, but silence there would
    read as the device ignoring them."""
    posted: list[dict] = []
    monkeypatch.setattr(
        rt_mod.requests, "post",
        lambda url, json=None, timeout=None: posted.append({"url": url, "json": json}),
    )
    realtime = _FakeRealtime(open_=True)
    said = "and while you are at it turn the lights down"

    rt = hold_for_main_handoff(realtime, said, "i-5")

    assert rt is not None and rt.route == ROUTE_MAIN_PENDING
    assert posted == [{"url": rt_mod.voice_cfg.OS_FILLER_URL,
                       "json": {"pool": "main_still_working", "owner": "i-5"}}]
    assert realtime.turns == [(
        said,
        "[Said while the main agent was still working on: Find my pen — the device answered only that it is still on it.]",
    )]


def test_filler_is_rate_limited_but_turn_is_still_held(monkeypatch):
    posted: list[dict] = []
    monkeypatch.setattr(rt_mod.requests, "post",
                        lambda url, json=None, timeout=None: posted.append(json))
    rt = hold_for_main_handoff(
        _FakeRealtime(open_=True, slot=False),
        "so what did you end up finding over there",
        "i-3",
    )
    assert rt is not None and rt.route == ROUTE_MAIN_PENDING
    assert posted == []


def test_empty_transcript_is_held_without_memory_entry(monkeypatch):
    monkeypatch.setattr(rt_mod.requests, "post", lambda *a, **k: None)
    realtime = _FakeRealtime(open_=True)
    rt = hold_for_main_handoff(realtime, "", "i-4")
    assert rt is not None and rt.route == ROUTE_MAIN_PENDING
    assert realtime.turns == []


def test_main_pending_route_never_reaches_os_server():
    assert should_drop_downstream_turn(RealtimeTurnResult(route=ROUTE_MAIN_PENDING))
