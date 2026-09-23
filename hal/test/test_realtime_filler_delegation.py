"""A spoken Gemini acknowledgement must not consume the main-agent handoff."""

import json

import pytest
from unittest.mock import Mock

from hal import config
from hal.drivers.voice._internal import realtime_turn, turn_dispatch
from hal.realtime.models import FunctionCallOutput, TextOutput
from hal.realtime.models.signal import DelegateSignal
from hal.realtime.orchestrator import RealtimeOrchestrator


REQUEST = "Hello? help me play a song"
FILLER = "I can help with that."


def _orchestrator(monkeypatch):
    monkeypatch.setattr(config, "REALTIME_SESSION_MAX_TURNS", 0)
    agent = Mock(execution_completed=False)
    agent.receive.return_value = iter([
        TextOutput(text=FILLER),
        FunctionCallOutput(
            name="delegate_to_main",
            arguments=json.dumps({"message": REQUEST}),
            call_id="play-song",
            user_transcript=REQUEST,
            user_turn_id="voice-turn-42",
        ),
        TextOutput(text="This must not become a second answer."),
    ])
    orchestrator = object.__new__(RealtimeOrchestrator)
    orchestrator._agent = agent
    orchestrator._skip_post_idle_recycle = False
    orchestrator._consecutive_silent = 0
    orchestrator._turns_since_recycle = 0
    orchestrator._idle_reset_pending = False
    return orchestrator, agent


def test_filler_then_delegate_emits_handoff_with_plain_ack(monkeypatch):
    orchestrator, agent = _orchestrator(monkeypatch)

    assert list(orchestrator.stream_output()) == [
        TextOutput(text=FILLER),
        DelegateSignal(
            message=REQUEST, transcript=REQUEST, user_turn_id="voice-turn-42",
        ),
    ]
    agent.end_turn.assert_called_once_with()
    agent.send.assert_called_once()
    ack, = agent.send.call_args.args[0]
    assert ack.call_id == "play-song"
    assert json.loads(ack.output) == {"result": "delegated"}
    assert "scheduling" not in ack.model_dump()


def test_spoken_filler_keeps_request_on_main_agent_route(monkeypatch):
    monkeypatch.setattr(config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(config, "REALTIME_NATIVE_AUDIO", False)
    monkeypatch.setattr(config, "REALTIME_PROVIDER", "gemini")
    monkeypatch.setattr(realtime_turn, "gemini_needs_idle_workaround", lambda: False)
    monkeypatch.setattr(realtime_turn, "_thinking_cue_start", lambda: None)
    monkeypatch.setattr(realtime_turn, "_thinking_cue_clear", lambda: None)
    monkeypatch.setattr(realtime_turn, "_reply_language_name", lambda: "English")
    monkeypatch.setattr(realtime_turn, "harness_followup_active", lambda: False)
    monkeypatch.setattr(turn_dispatch, "_take_vision_handoff", lambda **kwargs: ("", ""))
    orchestrator, _ = _orchestrator(monkeypatch)
    realtime = Mock(available=True, execution_completed=False)
    realtime.stream_output.side_effect = orchestrator.stream_output
    tts = Mock()

    result = realtime_turn.run_realtime_turn(
        realtime, tts, lambda text: text, REQUEST, [object()], 1.0,
        wait_filler=Mock(),
    )

    tts.speak.assert_called_once_with(FILLER, turn_id="", realtime_reply=True)
    assert result.route == realtime_turn.ROUTE_DELEGATED
    assert result.delegated
    assert not result.handled
    assert result.delegate_msg == REQUEST
    assert result.transcript == FILLER
    realtime.save_turn.assert_not_called()

    decorator = Mock()
    decorator.classify_wake_word.return_value = (REQUEST, "voice")
    decorated_request = f"Unknown Speaker: [voice:voice_42] {REQUEST}"
    decorator.identify_and_decorate.return_value = (decorated_request, "", "")
    sender = Mock()
    sender.send.return_value = None
    turn_dispatch.dispatch_turn(decorator, sender, REQUEST, [], [], result)

    sender.send.assert_called_once()
    message = sender.send.call_args.args[0]
    assert message.startswith(f"[voice-instruction] {REQUEST}\n[transcript] {decorated_request}\n")
    assert "[realtime-handoff]" in message
    assert "active request, not a handled history entry" in message
    assert "do not choose NO_REPLY merely because realtime already spoke" in message
    assert sender.send.call_args.kwargs["event_type"] == "voice"
    assert "[HANDLED]" not in message


def test_silent_delegation_keeps_existing_handoff(monkeypatch):
    monkeypatch.setattr(turn_dispatch, "_take_vision_handoff", lambda **kwargs: ("", ""))
    decorator = Mock()
    decorator.classify_wake_word.return_value = (REQUEST, "voice")
    decorator.identify_and_decorate.return_value = (REQUEST, "", "")
    sender = Mock()
    sender.send.return_value = None
    result = realtime_turn.RealtimeTurnResult(
        delegated=True, delegate_msg=REQUEST, route=realtime_turn.ROUTE_DELEGATED,
    )

    turn_dispatch.dispatch_turn(decorator, sender, REQUEST, [], [], result)

    sender.send.assert_called_once()
    assert sender.send.call_args.args[0] == (
        f"[voice-instruction] {REQUEST}\n[transcript] {REQUEST}"
    )


@pytest.mark.parametrize("delegate, ending", [(True, "."), (False, "."), (False, "")])
def test_provider_error_is_silent_but_preserves_delegation(monkeypatch, caplog, delegate, ending):
    import logging

    monkeypatch.setattr(config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(config, "REALTIME_NATIVE_AUDIO", False)
    monkeypatch.setattr(config, "REALTIME_FIRST_CHUNK_MAX_CHARS", 20)
    monkeypatch.setattr(realtime_turn, "gemini_needs_idle_workaround", lambda: False)
    monkeypatch.setattr(realtime_turn, "_thinking_cue_start", lambda: None)
    monkeypatch.setattr(realtime_turn, "_thinking_cue_clear", lambda: None)
    monkeypatch.setattr(realtime_turn, "_reply_language_name", lambda: "English")
    realtime = Mock(available=True, execution_completed=False)
    realtime.stream_output.return_value = iter([
        TextOutput(text="I'm sorry,"),
        TextOutput(text=" there was a system"),
        TextOutput(text=" error" + ending),
        *([DelegateSignal(message=REQUEST)] if delegate else []),
    ])
    tts = Mock()
    with caplog.at_level(logging.INFO, logger="hal.voice"):
        result = realtime_turn.run_realtime_turn(
            realtime, tts, lambda text: text, REQUEST, [object()], 1.0,
            wait_filler=Mock(), harness_followup=False,
        )
    tts.speak.assert_not_called()
    tts.speak_queue.assert_not_called()
    assert result.delegated == delegate
    assert not result.handled
    if delegate:
        assert result.delegate_msg == REQUEST
    else:
        assert result.route == realtime_turn.ROUTE_NO_OUTPUT
    assert result.transcript == ""
    assert "I'm sorry, there was a system error" in caplog.text


def test_error_filter_keeps_useful_answers():
    error = "I'm sorry, there was a system error."
    assert realtime_turn._filter_system_error_tts(error + " You are wearing orange.") == "You are wearing orange."
    assert realtime_turn._filter_system_error_tts("I'm sorry, I cannot see your shirt.") == "I'm sorry, I cannot see your shirt."
    assert realtime_turn._filter_system_error_tts("A system error means an operation failed.") == "A system error means an operation failed."
    assert realtime_turn._filter_system_error_tts(error[:-1]) == ""
    assert not realtime_turn._pending_system_error_tts("I'm sorry, I cannot see your shirt.")
