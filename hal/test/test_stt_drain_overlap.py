"""Confirmed authorized speech can reach realtime before CloseStream returns."""

import threading

import pytest

from hal.drivers.voice._internal.realtime_turn import RealtimeTurnResult
from hal.test.test_turn_endpoint_capture import capture


@pytest.mark.parametrize("wake_enabled,initial_focus", [(False, False), (True, True), (True, False)])
def test_confirmed_speech_replies_while_stt_drains(monkeypatch, wake_enabled, initial_focus):
    focus = [initial_focus]
    closing = threading.Event()
    replied = threading.Event()

    def read(elapsed):
        if elapsed == 1 and wake_enabled:
            focus[0] = True  # Gaze grants focus in the current capture.

    def close():
        closing.set()
        assert replied.wait(2), "realtime waited on STT close"

    def realtime(*args, **kwargs):
        assert closing.wait(2)
        assert kwargs["save_history"] is False
        replied.set()
        return RealtimeTurnResult(handled=True, transcript="Done.")

    with capture(
        monkeypatch, [(1, True, "Please check my memory"), (4, False, None)],
        realtime=True, wake_enabled=wake_enabled, focus=lambda: focus[0],
        on_read=read, on_close=close, on_realtime=realtime,
        close_transcript="and include yesterday",
    ) as result:
        assert replied.is_set()
        result.realtime.assert_called_once()
        result.stt.close.assert_called_once()
        result.metrics.speech_end.assert_called_once()
        result.service._realtime.save_turn.assert_called_once_with(
            user_text="Please check my memory and include yesterday", agent_text="Done.",
        )
        assert result.dispatch.call_args.args[2] == "Please check my memory and include yesterday"


def test_partial_only_input_keeps_transcript_gate(monkeypatch):
    closed = threading.Event()

    def realtime(*args, **kwargs):
        assert closed.is_set()
        assert "save_history" not in kwargs
        return RealtimeTurnResult()

    with capture(
        monkeypatch, [(1, True, "Please check my memory"), (4, False, None)],
        realtime=True, transcripts_final=False, on_close=closed.set, on_realtime=realtime,
    ) as result:
        result.realtime.assert_called_once()


def test_closed_wake_window_does_not_commit_confirmed_words(monkeypatch):
    with capture(
        monkeypatch, [(1, True, "Please check my memory"), (4, False, None)],
        realtime=True, wake_enabled=True, focus=lambda: False,
    ) as result:
        result.realtime.assert_not_called()
        result.dispatch.assert_not_called()
        result.stt.close.assert_called_once()


def test_punctuation_final_cannot_bypass_noise_gate(monkeypatch):
    with capture(
        monkeypatch, [(1, True, "..."), (4, False, None)], realtime=True,
    ) as result:
        # The ordinary path owns the noise verdict, never the early reply path.
        assert all("save_history" not in call.kwargs for call in result.realtime.call_args_list)
        result.stt.close.assert_called_once()


def test_slow_session_prepare_does_not_block_microphone(monkeypatch):
    preparing = threading.Event()
    captured = threading.Event()

    def prepare():
        preparing.set()
        assert captured.wait(2), "Gemini connect blocked microphone capture"

    def read(elapsed):
        if elapsed == 4:
            assert preparing.wait(2)
            captured.set()

    with capture(
        monkeypatch, [(1, True, "Please check my memory"), (4, False, None)],
        realtime=True, on_prepare=prepare, on_read=read,
    ) as result:
        result.service._realtime.prepare_turn.assert_called_once()
        assert result.stt.send_audio.call_count == 2
        assert result.service._realtime.append_audio.call_count == 2
        result.realtime.assert_called_once()


def test_harness_followup_keeps_complete_transcript(monkeypatch):
    from hal.drivers.voice import voice_service

    monkeypatch.setattr(voice_service, "harness_followup_active", lambda: True)
    with capture(
        monkeypatch, [(1, True, "Please check my memory"), (4, False, None)],
        realtime=True, close_transcript="and include yesterday",
    ) as result:
        result.realtime.assert_called_once()
        assert "save_history" not in result.realtime.call_args.kwargs
        assert result.realtime.call_args.args[3] == "Please check my memory and include yesterday"
