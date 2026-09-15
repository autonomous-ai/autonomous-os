"""Live LED state transitions must not change playback or measurement."""

import numpy as np
import pytest

from hal.drivers.voice._internal.live_cues import LiveVoiceCues
from hal.realtime.models.output import AudioOutput, TextOutput, UserSpeechOutput
from hal.test.test_live_voice_metrics import _pump
from hal.test.test_voice_metrics import kpi  # noqa: F401


def cues_fixture():
    now = [0.0]
    events = []
    cues = LiveVoiceCues(clock=lambda: now[0],
                         show=lambda owner, phase: events.append((owner, phase)),
                         clear=lambda owner: events.append((owner, "clear")))
    return cues, now, events


def test_live_listens_then_thinks_without_provider_endpoint():
    cues, now, events = cues_fixture()
    cues.speech()
    cues.speech()
    assert [e[1] for e in events] == ["listening"]
    now[0] = 0.9
    cues.tick()
    cues.input("turn")  # A late transcript is not a new listening phase.
    assert [e[1] for e in events] == ["listening", "thinking"]
    cues.finish("turn")
    assert events[-1][1] == "clear"
    cues.input("turn", endpoint_at=0.9)
    assert events[-1][1] == "clear"


def test_server_endpoint_changes_only_current_cue():
    cues, now, events = cues_fixture()
    cues.speech()
    cues.input("first", endpoint_at=0.1)
    assert events[-1][1] == "thinking"
    now[0] = 1.1
    cues.speech()
    newer_owner = events[-1][0]
    cues.finish("first")
    cues.input("first", endpoint_at=0.1)
    assert events[-1] == (newer_owner, "listening")
    cues.input("second", endpoint_at=1.2)
    cues.finish("first")
    assert events[-1] == (newer_owner, "thinking")
    cues.finish("second")
    assert events[-1] == (newer_owner, "clear")


def test_timeout_and_session_close_do_not_allow_late_repaint():
    cues, now, events = cues_fixture()
    cues.speech()
    cues.input("turn", endpoint_at=0.0)
    now[0] = 26.0
    cues.tick()
    assert events[-1][1] == "clear"
    cues.close()
    count = len(events)
    cues.speech()
    cues.input("next", endpoint_at=27)
    cues.tick()
    cues.finish("turn")
    assert len(events) == count


def test_led_failure_does_not_escape_into_voice_pipeline():
    def broken(*args):
        raise RuntimeError("LED unavailable")
    cues = LiveVoiceCues(show=broken, clear=broken)
    cues.speech()
    cues.input("turn", endpoint_at=0.0)
    cues.finish("turn")
    cues.close()


@pytest.mark.parametrize("native", [True, False])
def test_live_pump_cues_end_at_reply_without_extra_playback(monkeypatch, kpi, native):
    cues, _, events = cues_fixture()
    cues.speech()
    output = (AudioOutput(audio=np.zeros(16, dtype=np.float32), user_turn_id="a")
              if native else TextOutput(text="Hello there.", user_turn_id="a"))
    batches = [([UserSpeechOutput(turn_id="a", endpoint_at=0.2), output], "a", True)]
    spoken = _pump(monkeypatch, kpi, batches, native=native, cues=cues)
    assert [e[1] for e in events] == ["listening", "thinking", "clear"]
    if not native:
        assert len(spoken) == 1


def test_live_reuses_regular_hw_emotion_calls(monkeypatch):
    from hal import app_state
    from hal.drivers.voice.voice_service import VoiceService
    from hal.drivers.voice._internal import realtime_turn

    calls = []
    monkeypatch.setattr(VoiceService, "_set_emotion_local", lambda emotion: calls.append(emotion))
    monkeypatch.setattr(realtime_turn, "_thinking_cue_start", lambda: calls.append("thinking"))
    monkeypatch.setattr(realtime_turn, "_thinking_cue_clear", lambda: calls.append("clear_thinking"))
    monkeypatch.setattr(app_state, "clear_listening_cue", lambda: calls.append("clear_listening"))
    cues = LiveVoiceCues()
    try:
        cues.speech()
        cues.input("turn", endpoint_at=0.1)
        cues.finish("turn")
        assert calls == ["listening", "thinking", "clear_thinking", "clear_listening"]
    finally:
        cues.close()


def test_hw_effect_wait_does_not_block_mic_feedback(monkeypatch):
    import threading

    entered = threading.Event()
    release = threading.Event()

    def slow_emotion(*args):
        entered.set()
        assert release.wait(2)

    monkeypatch.setattr(LiveVoiceCues, "_show_emotion", staticmethod(slow_emotion))
    monkeypatch.setattr(LiveVoiceCues, "_clear_emotion", staticmethod(lambda owner: None))
    cues = LiveVoiceCues()
    try:
        cues.speech()
        assert entered.wait(1)
        # These return while the HW effect is still blocked on the worker.
        cues.speech()
        cues.tick()
        assert not release.is_set()
    finally:
        release.set()
        cues.close()
