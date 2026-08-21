"""Regression tests for the pending-tool-call audio gate (Gemini Live 1008).

Gemini refuses `send_realtime_input` while a tool call it emitted is unanswered
and closes the session with 1008 ("The operation was aborted"). These tests pin
the gate that keeps mic audio off the wire for that window — and, above all,
that the gate reopens on the fire-and-forget path, which never acknowledges the
call to Gemini and so has nothing else to reopen it.
"""

import asyncio
import queue
import threading
import time
from types import SimpleNamespace

import numpy as np

from hal.realtime.models import AudioInput, FunctionCallResultInput
from hal.realtime.voice_agent.gemini_live import GeminiLiveAgent


class _RecordingSession:
    """Captures what actually reached the wire."""

    def __init__(self) -> None:
        self.realtime_inputs: list[dict] = []
        self.tool_responses: list[object] = []

    async def send_realtime_input(self, **kwargs) -> None:
        self.realtime_inputs.append(kwargs)

    async def send_tool_response(self, function_responses) -> None:
        self.tool_responses.append(function_responses)

    async def receive(self):
        for message in self._messages:
            yield message


def _agent(session: _RecordingSession) -> GeminiLiveAgent:
    agent = object.__new__(GeminiLiveAgent)
    agent._session = session
    agent._config = SimpleNamespace(sample_rate=16000)
    agent._vad_disabled = False
    agent._activity_started = False
    agent._speech_ended_at = None
    agent._pending_tool_calls = set()
    agent._pending_tool_deadline = None
    agent._pending_tool_max_s = 10.0
    agent._gated_audio_frames = 0
    return agent


def _audio() -> AudioInput:
    return AudioInput(audio=np.zeros(160, dtype=np.float32))


def _tool_call_message(*call_ids: str) -> SimpleNamespace:
    return SimpleNamespace(
        usage_metadata=None,
        server_content=None,
        tool_call=SimpleNamespace(
            function_calls=[
                SimpleNamespace(name="express_emotion", args={}, id=call_id)
                for call_id in call_ids
            ]
        ),
        session_resumption_update=None,
        go_away=None,
    )


def test_audio_flows_when_no_tool_call_is_pending():
    session = _RecordingSession()
    agent = _agent(session)

    asyncio.run(agent._async_send_input(_audio()))

    assert len(session.realtime_inputs) == 1
    assert "audio" in session.realtime_inputs[0]


def test_receiving_a_tool_call_closes_the_audio_gate():
    session = _RecordingSession()
    agent = _agent(session)
    session._messages = [_tool_call_message("call-1")]
    agent._recv_queue = queue.Queue()
    agent._turn_done = threading.Event()
    agent._first_audio_received = False

    asyncio.run(agent._async_receive_turn())
    assert agent._pending_tool_calls == {"call-1"}

    asyncio.run(agent._async_send_input(_audio()))

    assert session.realtime_inputs == []
    assert agent._gated_audio_frames == 1


def test_fire_and_forget_result_reopens_the_gate_without_acking_gemini():
    """The path that would otherwise deafen the device for the whole session.

    trigger_response=False returns before send_tool_response on purpose (acking
    makes Gemini re-speak the reply), so the gate must be released before that
    early return, not by it.
    """
    session = _RecordingSession()
    agent = _agent(session)
    agent._pending_tool_calls = {"call-1"}
    agent._pending_tool_deadline = time.monotonic() + 10.0

    asyncio.run(
        agent._async_send_input(
            FunctionCallResultInput(
                call_id="call-1", output='{"result": "expressed"}', trigger_response=False
            )
        )
    )

    # Gemini was deliberately told nothing...
    assert session.tool_responses == []
    # ...but the mic is live again.
    assert agent._pending_tool_calls == set()
    asyncio.run(agent._async_send_input(_audio()))
    assert len(session.realtime_inputs) == 1


def test_acked_result_also_reopens_the_gate():
    session = _RecordingSession()
    agent = _agent(session)
    agent._pending_tool_calls = {"call-1"}
    agent._pending_tool_deadline = time.monotonic() + 10.0

    asyncio.run(
        agent._async_send_input(
            FunctionCallResultInput(
                call_id="call-1", output='{"result": "ok"}', trigger_response=True
            )
        )
    )

    assert len(session.tool_responses) == 1
    assert agent._pending_tool_calls == set()


def test_gate_stays_closed_until_every_parallel_call_resolves():
    session = _RecordingSession()
    agent = _agent(session)
    agent._pending_tool_calls = {"call-1", "call-2"}
    agent._pending_tool_deadline = time.monotonic() + 10.0

    asyncio.run(
        agent._async_send_input(
            FunctionCallResultInput(
                call_id="call-1", output="{}", trigger_response=False
            )
        )
    )
    asyncio.run(agent._async_send_input(_audio()))
    assert session.realtime_inputs == []

    asyncio.run(
        agent._async_send_input(
            FunctionCallResultInput(
                call_id="call-2", output="{}", trigger_response=False
            )
        )
    )
    asyncio.run(agent._async_send_input(_audio()))
    assert len(session.realtime_inputs) == 1


def test_unresolved_tool_call_expires_instead_of_muting_forever():
    """A handler that never answers must not cost the device its microphone."""
    session = _RecordingSession()
    agent = _agent(session)
    agent._pending_tool_calls = {"call-lost"}
    agent._pending_tool_deadline = time.monotonic() - 0.01  # already expired

    asyncio.run(agent._async_send_input(_audio()))

    assert len(session.realtime_inputs) == 1
    assert agent._pending_tool_calls == set()


def test_reconnect_drops_a_gate_belonging_to_the_dead_session():
    session = _RecordingSession()
    agent = _agent(session)
    agent._pending_tool_calls = {"call-1"}
    agent._pending_tool_deadline = time.monotonic() + 10.0

    agent._clear_pending_tool_calls()

    assert agent._pending_tool_calls == set()
    assert agent._pending_tool_deadline is None
    asyncio.run(agent._async_send_input(_audio()))
    assert len(session.realtime_inputs) == 1


def test_activity_start_is_gated_with_the_audio_it_brackets():
    """Manual VAD: no activityStart for frames we are not going to send."""
    session = _RecordingSession()
    agent = _agent(session)
    agent._vad_disabled = True
    agent._pending_tool_calls = {"call-1"}
    agent._pending_tool_deadline = time.monotonic() + 10.0

    asyncio.run(agent._async_send_input(_audio()))

    assert session.realtime_inputs == []
    assert agent._activity_started is False
