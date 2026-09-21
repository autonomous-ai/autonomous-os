"""NON_BLOCKING tool grace spans the SDK's per-turn receive iterators."""

import asyncio
import queue
import threading
from types import SimpleNamespace

import pytest
from google.genai.live import AsyncSession

from hal.realtime.models import FunctionCallOutput, OutputEvent, TextOutput, TurnDoneEvent
from hal.realtime.voice_agent import gemini_live
from hal.realtime.voice_agent.gemini_live import GeminiLiveAgent


class _Session:
    # Exercise the installed SDK's iterator boundary: receive() stops immediately
    # after yielding turn_complete, even while more frames await on the socket.
    receive = AsyncSession.receive

    def __init__(self, messages):
        self.messages = asyncio.Queue()
        for message in messages:
            self.messages.put_nowait(message)
        self.reads = 0

    async def _receive(self):
        self.reads += 1
        return await self.messages.get()


def _terminal(*, generation=False):
    content = SimpleNamespace(
        grounding_metadata=None, model_turn=None, output_transcription=None,
        interrupted=False, turn_complete=not generation,
        generation_complete=generation,
    )
    return SimpleNamespace(usage_metadata=None, server_content=content,
                           tool_call=None, session_resumption_update=None,
                           go_away=None, voice_activity=None)


def _tool(name="delegate_to_main", call_id="c1"):
    return SimpleNamespace(
        usage_metadata=None, server_content=None,
        tool_call=SimpleNamespace(function_calls=[
            SimpleNamespace(name=name, args={"message": "play a song"}, id=call_id)]),
        session_resumption_update=None, go_away=None, voice_activity=None)


def _agent(messages, model="gemini-3.8-live-extended-thinking"):
    agent = object.__new__(GeminiLiveAgent)
    agent._session = _Session(messages)
    agent._config = SimpleNamespace(model=model)
    agent._recv_queue = queue.Queue()
    agent._turn_done = threading.Event()
    agent._first_audio_received = False
    agent._pending_tool_calls = set()
    agent._turn_gen = 0
    agent._live_user_turn_id = "user-1"
    agent._user_transcript = "play a song"
    agent._awaiting_playback_turn_complete = False
    return agent


@pytest.fixture(autouse=True)
def _short_grace(monkeypatch):
    monkeypatch.setattr(gemini_live.app_config, "REALTIME_NONBLOCKING_TOOL_GRACE_S", 0.02)


def _receive(agent):
    # Bound the test independently so failure to finalize cannot hang pytest.
    asyncio.run(asyncio.wait_for(agent._async_receive_turn(), timeout=0.5))
    events = []
    while not agent._recv_queue.empty():
        events.append(agent._recv_queue.get_nowait())
    return events


def _assert_done(agent, events):
    terminals = [event for event in events if isinstance(event, TurnDoneEvent)]
    assert len(terminals) == 1
    assert terminals[0].user_turn_id == "user-1"
    assert agent._turn_done.is_set()
    assert agent._live_user_turn_id == ""


def _calls(events):
    return [event.output for event in events
            if isinstance(event, OutputEvent) and isinstance(event.output, FunctionCallOutput)]


@pytest.mark.parametrize("name", ["delegate_to_main", "reject_turn"])
def test_trailing_tool_call_caught_across_sdk_turn_boundary(name):
    agent = _agent([_terminal(), _tool(name)])
    events = _receive(agent)
    assert [call.name for call in _calls(events)] == [name]
    assert isinstance(events[-1], TurnDoneEvent)
    _assert_done(agent, events)


@pytest.mark.parametrize("generation", [False, True])
def test_no_tool_finalizes_after_grace_expires(generation):
    agent = _agent([_terminal(generation=generation)])
    events = _receive(agent)
    _assert_done(agent, events)
    assert agent._session.reads == 2
    assert not _calls(events)


def test_generation_complete_then_turn_complete_keeps_late_delegate_in_turn():
    agent = _agent([_terminal(generation=True), _terminal(), _tool()])
    events = _receive(agent)
    assert [call.name for call in _calls(events)] == ["delegate_to_main"]
    assert isinstance(events[-1], TurnDoneEvent)
    _assert_done(agent, events)


@pytest.mark.parametrize("during_grace", [False, True])
def test_auxiliary_tool_does_not_hide_trailing_delegate(during_grace):
    prefix = [_terminal(), _tool("get_weather", "aux")] if during_grace else [
        _tool("get_weather", "aux"), _terminal(),
    ]
    agent = _agent([*prefix, _tool()])
    events = _receive(agent)
    assert [call.name for call in _calls(events)] == ["get_weather", "delegate_to_main"]
    _assert_done(agent, events)


@pytest.mark.parametrize("generation", [False, True])
@pytest.mark.parametrize("plain_model", [False, True])
def test_plain_model_or_disabled_grace_finishes_immediately(monkeypatch, generation, plain_model):
    model = "gemini-3.8-live" if plain_model else "gemini-3.8-live-extended-thinking"
    if not plain_model:
        monkeypatch.setattr(gemini_live.app_config, "REALTIME_NONBLOCKING_TOOL_GRACE_S", 0)
    agent = _agent([_terminal(generation=generation), _tool()], model=model)
    events = _receive(agent)
    _assert_done(agent, events)
    assert agent._session.reads == 1
    assert not _calls(events)


def test_grace_timeout_leaves_session_usable_without_replaying_frames():
    async def scenario():
        agent = _agent([_terminal()])
        await asyncio.wait_for(agent._async_receive_turn(), timeout=0.5)
        first_done = agent._recv_queue.get_nowait()
        assert isinstance(first_done, TurnDoneEvent)
        assert first_done.user_turn_id == "user-1"
        assert agent._session.messages.empty()

        agent._live_user_turn_id = "user-2"
        agent._turn_done.clear()
        agent._session.messages.put_nowait(_terminal())
        agent._session.messages.put_nowait(_tool())
        await asyncio.wait_for(agent._async_receive_turn(), timeout=0.5)
        tool = agent._recv_queue.get_nowait()
        done = agent._recv_queue.get_nowait()
        assert isinstance(tool.output, FunctionCallOutput)
        assert tool.output.user_turn_id == "user-2"
        assert isinstance(done, TurnDoneEvent)
        assert done.user_turn_id == "user-2"
        assert agent._turn_done.is_set()
        assert agent._recv_queue.empty()
        assert agent._session.messages.empty()

    asyncio.run(scenario())


def test_filler_is_emitted_immediately_while_waiting_for_delayed_delegate(monkeypatch):
    monkeypatch.setattr(gemini_live.app_config, "REALTIME_NONBLOCKING_TOOL_GRACE_S", 5.0)

    class WaitingSession(_Session):
        def __init__(self, messages):
            super().__init__(messages)
            self.waiting_for_tool = asyncio.Event()

        async def _receive(self):
            if self.messages.empty():
                self.waiting_for_tool.set()
            return await super()._receive()

    async def scenario():
        filler = _terminal()
        filler.server_content.turn_complete = False
        filler.server_content.output_transcription = SimpleNamespace(text="I can help with that.")
        agent = _agent([])
        agent._session = WaitingSession([filler, _terminal()])
        task = asyncio.create_task(agent._async_receive_turn())
        try:
            # Synchronize on the next socket read, rather than relying on a sleep:
            # the terminal has arrived and grace is actively waiting for a tool.
            await asyncio.wait_for(agent._session.waiting_for_tool.wait(), timeout=1.0)
            assert not task.done()
            assert not agent._turn_done.is_set()
            early_events = []
            while not agent._recv_queue.empty():
                early_events.append(agent._recv_queue.get_nowait())
            texts = [event.output.text for event in early_events
                     if isinstance(event, OutputEvent) and isinstance(event.output, TextOutput)]
            assert texts == ["I can help with that."]
            assert not any(isinstance(event, TurnDoneEvent) for event in early_events)
            assert agent._user_transcript == "play a song"

            agent._session.messages.put_nowait(_tool())
            await asyncio.wait_for(task, timeout=1.0)
            call = agent._recv_queue.get_nowait()
            done = agent._recv_queue.get_nowait()
            assert isinstance(call.output, FunctionCallOutput)
            assert call.output.name == "delegate_to_main"
            assert call.output.user_transcript == "play a song"
            assert call.output.user_turn_id == "user-1"
            _assert_done(agent, [done])
            assert agent._recv_queue.empty()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
