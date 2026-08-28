"""Regression tests for the pending-tool-call session quarantine (Gemini Live 1008).

Gemini refuses client input while a tool call it emitted is unanswered and
closes the session with 1008 ("The operation was aborted"). A tool response is
the only way to make that session reusable. Fire-and-forget tools deliberately
do not send one, so their session must be rebuilt before another capture.
"""

import asyncio
import queue
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from hal.realtime.models import (
    AudioInput,
    FunctionCallResultInput,
    ImageInput,
    TextInput,
)
from hal.realtime.voice_agent.gemini_live import GeminiLiveAgent


class _RecordingSession:
    """Captures what actually reached the wire."""

    def __init__(self) -> None:
        self.realtime_inputs: list[dict] = []
        self.tool_responses: list[object] = []
        self.client_contents: list[dict] = []
        self.fail_tool_response = False

    async def send_realtime_input(self, **kwargs) -> None:
        self.realtime_inputs.append(kwargs)

    async def send_tool_response(self, function_responses) -> None:
        if self.fail_tool_response:
            raise RuntimeError("tool response send failed")
        self.tool_responses.append(function_responses)

    async def send_client_content(self, **kwargs) -> None:
        self.client_contents.append(kwargs)

    async def receive(self):
        for message in self._messages:
            yield message


def _agent(session: _RecordingSession) -> GeminiLiveAgent:
    agent = object.__new__(GeminiLiveAgent)
    agent._session = session
    agent._config = SimpleNamespace(sample_rate=16000)
    agent._vad_disabled = False
    agent._activity_started = False
    agent._last_audio_sent_at = None
    agent._activity_end_sent_at = None
    agent._pending_tool_calls = set()
    agent._gated_audio_frames = 0
    agent._turn_done = threading.Event()
    return agent


def _audio() -> AudioInput:
    return AudioInput(audio=np.zeros(160, dtype=np.float32))


def _frame() -> np.ndarray:
    return np.zeros((4, 4, 3), dtype=np.uint8)


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


def test_fire_and_forget_result_requires_a_fresh_session():
    """Skipping Gemini's acknowledgement makes this socket permanently unsafe."""
    session = _RecordingSession()
    agent = _agent(session)
    agent._pending_tool_calls = {"call-1"}

    asyncio.run(
        agent._async_send_input(
            FunctionCallResultInput(
                call_id="call-1", output='{"result": "expressed"}', trigger_response=False
            )
        )
    )

    # Gemini was deliberately told nothing, so the original call remains
    # unresolved from the server's point of view.
    assert session.tool_responses == []
    assert agent._pending_tool_calls == {"call-1"}
    assert agent.requires_fresh_session is True

    # No client input may touch a session Gemini is still waiting on.
    asyncio.run(agent._async_send_input(_audio()))
    asyncio.run(agent._async_send_input(TextInput(text="turn context")))
    assert session.realtime_inputs == []
    assert session.client_contents == []


def test_acked_result_reopens_the_gate_only_after_the_send_succeeds():
    session = _RecordingSession()
    agent = _agent(session)
    agent._pending_tool_calls = {"call-1"}

    asyncio.run(
        agent._async_send_input(
            FunctionCallResultInput(
                call_id="call-1", output='{"result": "ok"}', trigger_response=True
            )
        )
    )

    assert len(session.tool_responses) == 1
    assert agent._pending_tool_calls == set()
    assert agent.requires_fresh_session is False


def test_failed_tool_response_keeps_the_session_non_reusable():
    session = _RecordingSession()
    session.fail_tool_response = True
    agent = _agent(session)
    agent._pending_tool_calls = {"call-1"}

    with pytest.raises(RuntimeError, match="tool response send failed"):
        asyncio.run(
            agent._async_send_input(
                FunctionCallResultInput(
                    call_id="call-1", output='{"result": "ok"}', trigger_response=True
                )
            )
        )

    assert session.tool_responses == []
    assert agent._pending_tool_calls == {"call-1"}
    assert agent.requires_fresh_session is True


def test_gate_stays_closed_until_every_parallel_call_is_acknowledged():
    session = _RecordingSession()
    agent = _agent(session)
    agent._pending_tool_calls = {"call-1", "call-2"}

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
            FunctionCallResultInput(call_id="call-2", output="{}", trigger_response=True)
        )
    )
    asyncio.run(agent._async_send_input(_audio()))
    assert session.realtime_inputs == []
    assert agent._pending_tool_calls == {"call-1"}
    assert agent.requires_fresh_session is True


def test_unresolved_tool_call_never_expires_into_sending_input():
    """Only rebuilding the session can recover an unanswered Gemini tool call."""
    session = _RecordingSession()
    agent = _agent(session)
    agent._pending_tool_calls = {"call-lost"}

    asyncio.run(agent._async_send_input(_audio()))
    asyncio.run(agent._async_send_input(TextInput(text="turn context")))

    assert session.realtime_inputs == []
    assert session.client_contents == []
    assert agent._pending_tool_calls == {"call-lost"}
    assert agent.requires_fresh_session is True


def test_reconnect_drops_a_gate_belonging_to_the_dead_session():
    session = _RecordingSession()
    agent = _agent(session)
    agent._pending_tool_calls = {"call-1"}

    agent._clear_pending_tool_calls()

    assert agent._pending_tool_calls == set()
    asyncio.run(agent._async_send_input(_audio()))
    assert len(session.realtime_inputs) == 1


def test_activity_start_is_gated_with_the_audio_it_brackets():
    """Manual VAD: no activityStart for frames we are not going to send."""
    session = _RecordingSession()
    agent = _agent(session)
    agent._vad_disabled = True
    agent._pending_tool_calls = {"call-1"}

    asyncio.run(agent._async_send_input(_audio()))

    assert session.realtime_inputs == []
    assert agent._activity_started is False


def test_activity_end_is_suppressed_while_a_tool_call_is_pending():
    """Manual VAD must not emit activityEnd into an unresolved tool turn."""
    session = _RecordingSession()
    agent = _agent(session)
    agent._vad_disabled = True
    agent._activity_started = True
    agent._pending_tool_calls = {"call-1"}

    asyncio.run(agent._async_commit())

    assert session.realtime_inputs == []
    # The bracket belongs to the dying session, so do not carry it into its
    # replacement.
    assert agent._activity_started is False
    assert agent.requires_fresh_session is True


def test_an_image_is_dropped_while_a_tool_call_is_pending():
    """The gate is not audio-only: ImageInput rides send_realtime_input too.

    This is the `look` flow's frame. It shares the API Gemini refuses while a
    call is unanswered, so it must be gated for the same reason audio is —
    otherwise the session dies with 1008 instead of losing one frame.
    """
    session = _RecordingSession()
    agent = _agent(session)
    agent._pending_tool_calls = {"call-1"}

    asyncio.run(agent._async_send_input(ImageInput(image=_frame())))

    assert session.realtime_inputs == []
    # Not audio, so it must not be counted as a gated audio frame.
    assert agent._gated_audio_frames == 0


def test_an_image_flows_once_the_tool_call_has_been_answered():
    """What the look flow depends on: ack first, then the frame goes out.

    The fresh-frame path used to send no result at all and relied on the gate's
    10s expiry outliving an 8s aim — flaky by construction, and broken outright
    once the expiry was removed. Acknowledging the call is what reopens
    send_realtime_input for both the frame and the replayed audio.
    """
    session = _RecordingSession()
    agent = _agent(session)
    agent._pending_tool_calls = {"call-1"}

    asyncio.run(
        agent._async_send_input(
            FunctionCallResultInput(
                call_id="call-1",
                output='{"result": "frame incoming; wait for the image"}',
                trigger_response=True,
            )
        )
    )
    assert agent._pending_tool_calls == set()

    asyncio.run(agent._async_send_input(ImageInput(image=_frame())))
    assert len(session.realtime_inputs) == 1
    assert "video" in session.realtime_inputs[0]


def test_replayed_audio_flows_once_the_tool_call_has_been_answered():
    """The other half of the look flow: the user's utterance is sent twice.

    The replay re-appends the SAME mic frames so the queued image joins the
    question. Those are AudioInput, so an unanswered call silences the replay
    as surely as it drops the frame — the model then answers from nothing.
    """
    session = _RecordingSession()
    agent = _agent(session)
    agent._pending_tool_calls = {"call-1"}

    asyncio.run(agent._async_send_input(_audio()))
    assert session.realtime_inputs == []

    asyncio.run(
        agent._async_send_input(
            FunctionCallResultInput(call_id="call-1", output="{}", trigger_response=True)
        )
    )
    asyncio.run(agent._async_send_input(_audio()))

    assert len(session.realtime_inputs) == 1
    assert "audio" in session.realtime_inputs[0]


# --- The look ack tells the model whether the aim found the user ---


def _ack_payload(res):
    """The dict the orchestrator would send as the look tool's result."""
    import json as _json

    found_user = bool(res is None or getattr(res, "aimed", False)
                      or getattr(res, "reason", "") != "subject not found")
    ack = {"result": "frame incoming; wait for the image"}
    if not found_user:
        ack["found_user"] = False
        ack["note"] = "..."
    return _json.loads(_json.dumps(ack))


class _Aim:
    def __init__(self, aimed, reason):
        self.aimed = aimed
        self.reason = reason


def test_a_centred_aim_reports_nothing_unusual():
    assert "found_user" not in _ack_payload(_Aim(True, "centred on person"))


def test_running_out_of_time_still_counts_as_finding_them():
    """The person IS in frame on these exits — just not centred yet. Flagging
    them would make the model hedge on a perfectly good picture."""
    assert "found_user" not in _ack_payload(_Aim(False, "deadline"))
    assert "found_user" not in _ack_payload(_Aim(False, "max iterations"))


def test_finding_nobody_is_reported_to_the_model():
    """Without this the model cannot tell a framed shot from "wherever the
    camera happened to be pointing", and answers both with equal confidence."""
    ack = _ack_payload(_Aim(False, "subject not found"))
    assert ack["found_user"] is False


def test_a_disabled_aim_makes_no_claim_either_way():
    """LOOK_AIM_ENABLED off, or the aim raised — no aim ran, so nothing is
    known about framing and the model should not be told otherwise."""
    assert "found_user" not in _ack_payload(None)
