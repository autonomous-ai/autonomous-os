"""Live-path half of the #419 guard: VAD triggers and confirmed openers hold
while the main agent is still working on a delegated request."""

from hal.drivers.voice import voice_service as vs_mod
from hal.drivers.voice.voice_service import VoiceService


class _HandoffRealtime:
    def __init__(self, open_: bool) -> None:
        self._open = open_
        self.closed: list[str] = []
        self.prepared = 0
        self.turns: list[tuple[str, str]] = []

    def main_handoff_open(self) -> bool:
        return self._open

    def main_handoff_transcript(self) -> str:
        return "Find my pen"

    def take_main_handoff_filler_slot(self) -> bool:
        return self._open

    def close_main_handoff(self, reason: str) -> bool:
        self.closed.append(reason)
        was = self._open
        self._open = False
        return was

    def prepare_turn(self) -> None:
        self.prepared += 1

    def wait_until_available(self, timeout: float) -> bool:
        return True

    def save_turn(self, user_text: str, agent_text: str) -> None:
        self.turns.append((user_text, agent_text))


def _service(realtime) -> VoiceService:
    svc = object.__new__(VoiceService)
    svc._realtime = realtime
    return svc


def test_live_decision_holds_while_handoff_open(monkeypatch):
    monkeypatch.setattr(vs_mod.hal_config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(vs_mod.hal_config, "WAKEWORD_ENABLED", False)
    monkeypatch.setattr(VoiceService, "_music_is_playing", lambda self: False)
    posted: list[dict] = []
    monkeypatch.setattr(vs_mod.realtime_turn_mod.requests, "post",
                        lambda url, json=None, timeout=None: posted.append(json))
    realtime = _HandoffRealtime(open_=True)

    assert _service(realtime)._live_decision([]) == "hold"
    assert realtime.prepared == 0, "must not touch the provider session"
    assert posted == [{"pool": "main_still_working", "owner": ""}]


def test_live_decision_unchanged_when_no_handoff(monkeypatch):
    monkeypatch.setattr(vs_mod.hal_config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(vs_mod.hal_config, "WAKEWORD_ENABLED", False)
    monkeypatch.setattr(VoiceService, "_music_is_playing", lambda self: False)
    realtime = _HandoffRealtime(open_=False)

    assert _service(realtime)._live_decision([]) == "live"
    assert realtime.prepared == 1


def test_live_opener_consumed_by_hold(monkeypatch):
    monkeypatch.setattr(vs_mod.voice_cfg, "LIVE_MODE", True)
    monkeypatch.setattr(vs_mod.hal_config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(vs_mod, "bypass_realtime", lambda hv: False)
    monkeypatch.setattr(vs_mod.realtime_turn_mod.requests, "post", lambda *a, **k: None)
    realtime = _HandoffRealtime(open_=True)

    consumed, reopen = _service(realtime)._try_live_opener(
        None, 320, 16000, [b"\x00\x00"],
        transcript="sếp sếp ơi", interaction_id="i-9", harness_voice=None,
    )

    assert (consumed, reopen) == (True, False)
    assert realtime.prepared == 0
    assert realtime.turns and realtime.turns[0][0] == "sếp sếp ơi"


def test_release_main_handoff_delegates_to_orchestrator():
    realtime = _HandoffRealtime(open_=True)
    assert _service(realtime).release_main_handoff("click") is True
    assert realtime.closed == ["click"]


def test_unaddressed_speech_in_wakeword_mode_is_not_held(monkeypatch):
    """The hold sits after the wake-word gate, not before it.

    A handoff being open must not turn every VAD trigger into a spoken "still
    on it": with wake-word mode on and the focus window closed, the speech was
    not addressed to the device (the user talking to somebody else while the
    lamp works). That takes the ordinary STT path and stays silent.
    """
    monkeypatch.setattr(vs_mod.hal_config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(vs_mod.hal_config, "WAKEWORD_ENABLED", True)
    monkeypatch.setattr(VoiceService, "_music_is_playing", lambda self: False)
    posted: list[dict] = []
    monkeypatch.setattr(vs_mod.realtime_turn_mod.requests, "post",
                        lambda url, json=None, timeout=None: posted.append(json))
    realtime = _HandoffRealtime(open_=True)
    svc = _service(realtime)

    class _ClosedFocus:
        def is_active(self) -> bool:
            return False

    svc._wakeword_focus = _ClosedFocus()

    assert svc._live_decision([]) == "turn"
    assert posted == [], "unaddressed speech must not be answered with a filler"
