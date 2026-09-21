"""Missing explicit Gemini outcome delegates even after spoken acknowledgement."""

from unittest.mock import Mock

import pytest

from hal import config
from hal.drivers.voice._internal import realtime_turn
from hal.realtime.models import (
    MainAgentFallbackOutput, OutputEvent, TextOutput, TurnDoneEvent,
)
from hal.realtime.models.signal import DelegateSignal
from hal.realtime.orchestrator import RealtimeOrchestrator
from hal.realtime.voice_agent.base import VoiceAgentBase


REQUEST = "Bật đèn giúp tôi"
FILLER = "I can help with that. Turning the light on now."


class Agent(VoiceAgentBase):
    @property
    def sample_rate(self):
        return 16000

    def _do_connect(self): ...
    def _do_disconnect(self): ...
    def _send_loop(self): ...
    def _recv_loop(self): ...


def _agent_with_missing_outcome():
    agent = Agent()
    agent._recv_queue.put(OutputEvent(output=TextOutput(text=FILLER)))
    agent._recv_queue.put(TurnDoneEvent(
        # A provider terminal must not override the explicit fallback decision.
        execution_completed=True,
        fallback_to_main=True,
        user_transcript=REQUEST,
        user_turn_id="turn-42",
    ))
    return agent


@pytest.mark.parametrize("stop_on_done", [True, False])
def test_terminal_fallback_retains_request_and_never_completes(stop_on_done):
    agent = _agent_with_missing_outcome()
    outputs = agent.receive(stop_on_done=stop_on_done)
    assert next(outputs) == TextOutput(text=FILLER)
    assert next(outputs) == MainAgentFallbackOutput(
        transcript=REQUEST, user_turn_id="turn-42",
    )
    assert not agent.execution_completed
    assert agent.execution_turn_id == "turn-42"
    if stop_on_done:
        assert list(outputs) == []
    else:
        outputs.close()


def test_ordinary_provider_terminal_keeps_existing_completion_behavior():
    agent = Agent()
    agent._recv_queue.put(TurnDoneEvent(execution_completed=True))
    assert list(agent.receive()) == []
    assert agent.execution_completed


def _orchestrator(agent):
    orchestrator = object.__new__(RealtimeOrchestrator)
    orchestrator._agent = agent
    orchestrator._skip_post_idle_recycle = False
    orchestrator._consecutive_silent = 0
    orchestrator._turns_since_recycle = 0
    orchestrator._idle_reset_pending = False
    return orchestrator


def test_orchestrator_fallback_delegates_without_function_ack(monkeypatch):
    monkeypatch.setattr(config, "REALTIME_SESSION_MAX_TURNS", 0)
    agent = _agent_with_missing_outcome()
    agent.end_turn = Mock()
    orchestrator = _orchestrator(agent)
    assert list(orchestrator.stream_output()) == [
        TextOutput(text=FILLER),
        DelegateSignal(message=REQUEST, transcript=REQUEST, user_turn_id="turn-42"),
    ]
    agent.end_turn.assert_called_once_with()
    assert agent._send_queue.empty()
    assert not orchestrator.execution_completed


def test_missing_outcome_after_spoken_filler_is_not_handled(monkeypatch):
    monkeypatch.setattr(config, "REALTIME_SESSION_MAX_TURNS", 0)
    monkeypatch.setattr(config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(config, "REALTIME_NATIVE_AUDIO", False)
    monkeypatch.setattr(config, "REALTIME_PROVIDER", "gemini")
    monkeypatch.setattr(realtime_turn, "gemini_needs_idle_workaround", lambda: False)
    monkeypatch.setattr(realtime_turn, "_thinking_cue_start", lambda: None)
    monkeypatch.setattr(realtime_turn, "_thinking_cue_clear", lambda: None)
    monkeypatch.setattr(realtime_turn, "_reply_language_name", lambda: "English")
    monkeypatch.setattr(realtime_turn, "harness_followup_active", lambda: False)
    orchestrator = _orchestrator(_agent_with_missing_outcome())
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
    assert not result.execution_completed
    assert result.delegate_msg == REQUEST
    realtime.save_turn.assert_not_called()
