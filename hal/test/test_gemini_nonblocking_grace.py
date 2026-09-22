"""NON_BLOCKING tool grace spans the SDK's per-turn receive iterators."""

import asyncio
import queue
import threading
from types import SimpleNamespace

import pytest
from google.genai.live import AsyncSession

from hal.realtime.models import AudioOutput, FunctionCallOutput, OutputEvent, TextOutput, TurnDoneEvent
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
        self.tool_responses = []
        self.control_turns = []

    async def _receive(self):
        self.reads += 1
        return await self.messages.get()

    async def send_client_content(self, **kwargs):
        self.control_turns.append(kwargs)

    async def send_tool_response(self, *, function_responses):
        self.tool_responses.extend(function_responses)


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
    agent._last_audio_sent_at = None
    agent._activity_end_sent_at = None
    agent._pending_tool_calls = set()
    agent._turn_gen = 0
    agent._live_user_turn_id = "user-1"
    agent._user_transcript = "play a song"
    agent._awaiting_playback_turn_complete = False
    return agent


@pytest.fixture(autouse=True)
def _short_grace(monkeypatch):
    monkeypatch.setattr(gemini_live.app_config, "REALTIME_NONBLOCKING_TOOL_GRACE_S", 0.02)
    monkeypatch.setattr(gemini_live.app_config, "REALTIME_OUTCOME_TIMEOUT_S", 0.03)


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
    assert not events[-1].fallback_to_main
    _assert_done(agent, events)


@pytest.mark.parametrize("generation", [False, True])
def test_no_tool_finalizes_after_grace_expires(generation):
    agent = _agent([_terminal(generation=generation)])
    events = _receive(agent)
    _assert_done(agent, events)
    assert agent._session.reads == 2
    assert not _calls(events)
    assert events[-1].fallback_to_main
    assert not events[-1].execution_completed
    assert events[-1].user_transcript == "play a song"


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
    assert events[-1].fallback_to_main is not plain_model
    assert events[-1].execution_completed is plain_model


@pytest.mark.parametrize("spoken", ["I can help with that.", "Sorry, there was a system error."])
@pytest.mark.parametrize("generation", [False, True])
def test_spoken_filler_or_error_without_outcome_falls_back_with_original_request(spoken, generation):
    response = _terminal(generation=generation)
    response.server_content.output_transcription = SimpleNamespace(text=spoken)
    agent = _agent([response])
    events = _receive(agent)
    assert any(isinstance(event, OutputEvent) and isinstance(event.output, TextOutput)
               and event.output.text == spoken for event in events)
    done = events[-1]
    assert isinstance(done, TurnDoneEvent)
    assert done.fallback_to_main
    assert not done.execution_completed
    assert done.user_transcript == "play a song"
    _assert_done(agent, events)


@pytest.mark.parametrize("after_terminal", [False, True])
def test_complete_response_is_acknowledged_internally_and_confirms_direct_answer(after_terminal):
    response = _terminal()
    response.server_content.turn_complete = False
    response.server_content.output_transcription = SimpleNamespace(text="Two plus two is four.")
    completion = _tool("complete_response", "direct-answer")
    completion.tool_call.function_calls[0].args = {}
    suffix = [_terminal(), completion] if after_terminal else [completion, _terminal()]
    agent = _agent([response, *suffix])
    agent._user_transcript = "What is two plus two?"
    events = _receive(agent)
    assert not _calls(events)
    assert not events[-1].fallback_to_main
    assert events[-1].execution_completed
    assert not agent._pending_tool_calls
    response, = agent._session.tool_responses
    assert response.name == "complete_response"
    assert response.id == "direct-answer"
    assert response.response == {"result": "recorded"}
    assert response.scheduling is None
    _assert_done(agent, events)


def test_auxiliary_tool_without_outcome_preserves_request_for_fallback():
    agent = _agent([_tool("express_emotion", "emotion"), _terminal()])
    events = _receive(agent)
    assert events[-1].fallback_to_main
    assert not events[-1].execution_completed
    assert events[-1].user_transcript == "play a song"


def test_delegate_after_complete_response_wins_and_keeps_original_request():
    completion = _tool("complete_response", "premature-completion")
    completion.tool_call.function_calls[0].args = {}
    agent = _agent([_terminal(), completion, _tool()])
    events = _receive(agent)

    calls = _calls(events)
    assert len(calls) == 1
    assert calls[0].name == "delegate_to_main"
    assert calls[0].user_transcript == "play a song"
    assert calls[0].user_turn_id == "user-1"
    assert isinstance(events[-1], TurnDoneEvent)
    assert not events[-1].fallback_to_main
    _assert_done(agent, events)
    assert agent._session.messages.empty()
    response, = agent._session.tool_responses
    assert response.name == "complete_response"
    assert response.id == "premature-completion"


def test_completion_ack_does_not_repeat_spoken_answer_or_hide_later_delegate():
    def answer():
        message = _terminal()
        message.server_content.turn_complete = False
        message.server_content.output_transcription = SimpleNamespace(text="Two plus two is four.")
        return message

    completion = _tool("complete_response", "direct-answer")
    completion.tool_call.function_calls[0].args = {}
    agent = _agent([answer(), _terminal(), completion, answer(), _tool()])
    events = _receive(agent)
    texts = [event.output.text for event in events
             if isinstance(event, OutputEvent) and isinstance(event.output, TextOutput)]
    assert texts == ["Two plus two is four."]
    call, = _calls(events)
    assert call.name == "delegate_to_main"
    assert call.user_transcript == "play a song"
    assert isinstance(events[-1], TurnDoneEvent)
    assert not events[-1].fallback_to_main
    _assert_done(agent, events)


@pytest.mark.parametrize('generation', [False, True])
@pytest.mark.parametrize('initial_result,final_result,delegate,terminal,release', [
    (False, True, False, True, True),
    (False, True, True, True, False),
    (False, False, False, True, False),
    (False, None, False, True, False),
    (True, True, False, True, False),
    (None, True, False, True, False),
    (False, True, False, False, False),
    (False, True, False, 'confirmation', True),
    (False, True, True, 'confirmation', False),
    (None, True, False, 'confirmation', False),
])
def test_continuation_after_filler_waits_for_outcome_and_late_routing(
        monkeypatch, generation, initial_result, final_result, delegate, terminal, release):
    from hal.realtime import response_outcome
    monkeypatch.setattr(gemini_live.app_config, 'REALTIME_NONBLOCKING_TOOL_GRACE_S', 0.08)
    monkeypatch.setattr(gemini_live.app_config, 'REALTIME_OUTCOME_TIMEOUT_S', 0.12)

    filler = 'Let me check that weather for you.'
    answer = 'Ho Chi Minh City is rainy today.'

    def speech(text):
        message = _terminal()
        message.server_content.turn_complete = False
        message.server_content.output_transcription = SimpleNamespace(text=text)
        message.server_content.model_turn = SimpleNamespace(parts=[
            SimpleNamespace(thought=False, text=None,
                            inline_data=SimpleNamespace(data=b'\x00\x00' * 4)),
        ])
        return message

    messages = [speech(filler), _terminal(generation=generation), speech(answer)]
    if terminal == 'confirmation':
        messages.append(_tool('complete_response'))
    elif terminal:
        messages.append(_terminal(generation=generation))
    if delegate:
        messages.append(_tool())
    agent = _agent(messages)
    agent._user_transcript = 'What is the weather in Ho Chi Minh City?'
    observed = []

    async def check(request, spoken, **kwargs):
        # Even while the classifier runs, the candidate must stay off-speaker.
        queued = list(agent._recv_queue.queue)
        assert not any(isinstance(e, OutputEvent) and isinstance(e.output, TextOutput)
                       and e.output.text == answer for e in queued)
        observed.append(spoken)
        return initial_result if spoken == filler else final_result

    monkeypatch.setattr(response_outcome, 'spoken_response_complete', check)
    events = _receive(agent)
    texts = [e.output.text for e in events if isinstance(e, OutputEvent)
             and isinstance(e.output, TextOutput)]
    assert texts == ([filler, answer] if release else [filler])
    audio = [e for e in events if isinstance(e, OutputEvent) and isinstance(e.output, AudioOutput)]
    assert len(audio) == (2 if release else 1)
    assert bool(_calls(events)) is delegate
    assert events[-1].fallback_to_main is (not delegate and not release and initial_result is not True)
    assert events[-1].user_transcript == 'What is the weather in Ho Chi Minh City?'
    if release:
        assert observed == [filler, filler + answer]
    _assert_done(agent, events)


@pytest.mark.parametrize('delegate', [False, True])
@pytest.mark.parametrize('spoken', [False, True])
def test_spoken_handoff_quarantines_before_consumer_can_start_next_capture(
        monkeypatch, delegate, spoken):
    from hal.realtime import response_outcome

    async def check(*args, **kwargs):
        return False

    monkeypatch.setattr(response_outcome, 'spoken_response_complete', check)
    messages = []
    if spoken:
        message = _terminal()
        message.server_content.turn_complete = False
        message.server_content.output_transcription = SimpleNamespace(text='Let me check.')
        messages.append(message)
    messages.append(_tool() if delegate else _terminal())
    if delegate:
        messages.append(_terminal())
    agent = _agent(messages)
    boundaries = []

    class ObservedQueue(queue.Queue):
        def put(self, event):
            if isinstance(event, TurnDoneEvent) or (
                    isinstance(event, OutputEvent) and isinstance(event.output, FunctionCallOutput)):
                boundaries.append(getattr(agent, '_requires_fresh_session', False))
            super().put(event)

    agent._recv_queue = ObservedQueue()
    _receive(agent)
    assert boundaries
    assert all(value is spoken for value in boundaries)


@pytest.mark.parametrize("generation", [False, True])
@pytest.mark.parametrize("outcome", ["complete_response", "delegate_to_main", None])
def test_first_terminal_stops_extra_speech_and_audio_but_keeps_routing(monkeypatch, generation, outcome):
    from hal.realtime import response_outcome
    direct_answer = outcome == "complete_response"

    async def check(*args, **kwargs):
        return direct_answer

    monkeypatch.setattr(response_outcome, 'spoken_response_complete', check)
    initial_text = "Two plus two is four." if direct_answer else "I can help with that."
    extra_text = "Sorry, there was a system error." if direct_answer else "I cannot access your email."
    request = "What is two plus two?" if direct_answer else "Read my newest email."

    def spoken(text):
        message = _terminal()
        message.server_content.turn_complete = False
        message.server_content.output_transcription = SimpleNamespace(text=text)
        message.server_content.model_turn = SimpleNamespace(parts=[
            SimpleNamespace(thought=False, text=None,
                            inline_data=SimpleNamespace(data=b"\x00\x00" * 4)),
        ])
        return message

    messages = [spoken(initial_text), _terminal(generation=generation), spoken(extra_text)]
    if outcome is not None:
        tool = _tool(outcome)
        tool.tool_call.function_calls[0].args = {} if direct_answer else {"message": request}
        messages.append(tool)
    agent = _agent(messages)
    agent._user_transcript = request
    events = _receive(agent)
    outputs = [event.output for event in events if isinstance(event, OutputEvent)]
    assert [output.text for output in outputs if isinstance(output, TextOutput)] == [initial_text]
    assert len([output for output in outputs if isinstance(output, AudioOutput)]) == 1
    assert events[-1].fallback_to_main is (outcome is None)
    assert events[-1].execution_completed is (outcome is not None)
    assert events[-1].user_transcript == request
    if outcome == "delegate_to_main":
        call, = _calls(events)
        assert call.name == outcome
        assert call.user_transcript == request
    else:
        assert not _calls(events)
    _assert_done(agent, events)


@pytest.mark.parametrize("extended", [False, True])
def test_fail_fast_preserves_request_and_explicitly_falls_back_for_extended(extended):
    model = "gemini-3.8-live-extended-thinking" if extended else "gemini-3.1-flash-live-preview"
    agent = _agent([], model=model)
    agent._fail_fast_turn("test connection closed")
    done = agent._recv_queue.get_nowait()
    assert isinstance(done, TurnDoneEvent)
    assert done.fallback_to_main is extended
    assert not done.execution_completed
    assert done.user_transcript == "play a song"
    assert done.user_turn_id == "user-1"
    assert agent._turn_done.is_set()
    assert agent._recv_queue.empty()
    agent._fail_fast_turn("duplicate close notification")
    assert agent._recv_queue.empty()


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


@pytest.mark.parametrize('complete', [True, False])
@pytest.mark.parametrize('generation', [True, False])
def test_independent_outcome_preserves_spoken_turn_and_route(monkeypatch, complete, generation):
    from hal.realtime import response_outcome
    observed = []

    async def check(request, answer, **kwargs):
        observed.append((request, answer))
        return complete

    monkeypatch.setattr(response_outcome, 'spoken_response_complete', check)
    speech = _terminal(generation=generation)
    speech.server_content.output_transcription = SimpleNamespace(text='Cali is rainy. Otters hold hands.')
    agent = _agent([speech, _terminal()])
    agent._user_transcript = 'Tell me something fun and check the weather in Cali.'
    events = _receive(agent)
    done = next(e for e in events if isinstance(e, TurnDoneEvent))
    assert observed == [('Tell me something fun and check the weather in Cali.',
                         'Cali is rainy. Otters hold hands.')]
    assert done.execution_completed is complete
    assert done.fallback_to_main is (not complete)
    assert done.user_turn_id == 'user-1'
    assert done.user_transcript == observed[0][0]
    texts = [e.output.text for e in events if isinstance(e, OutputEvent)
             and isinstance(e.output, TextOutput)]
    assert texts == [observed[0][1]]
    assert not agent._session.control_turns


def test_late_delegate_wins_over_independent_completion(monkeypatch):
    from hal.realtime import response_outcome

    async def check(*args, **kwargs):
        return True

    monkeypatch.setattr(response_outcome, 'spoken_response_complete', check)
    speech = _terminal()
    speech.server_content.output_transcription = SimpleNamespace(text='Here is a fun fact.')
    agent = _agent([speech, _tool()])
    events = _receive(agent)
    calls = [e.output.name for e in events if isinstance(e, OutputEvent)
             and isinstance(e.output, FunctionCallOutput)]
    assert calls == ['delegate_to_main']


@pytest.mark.parametrize('generation', [True, False])
@pytest.mark.parametrize('verdict,needs_main', [
    ('CLARIFICATION', False), ('INCOMPLETE', True), ('unexpected', True),
])
def test_weather_clarification_routes_through_real_outcome_parser(
        monkeypatch, generation, verdict, needs_main):
    import anthropic
    import json
    from hal.realtime import response_outcome

    request = 'Can you tell me the weather today and Bitcoin pricing?'
    answer = ("Bitcoin is trading at around 86,400 USD today. "
              "To give you the correct weather, I'll need to know which city you're in.")
    captured = []

    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        @property
        def text_stream(self):
            async def chunks():
                yield verdict
            return chunks()

    class Client(Stream):
        def __init__(self, **kwargs):
            self.messages = self

        def stream(self, **kwargs):
            captured.append(json.loads(kwargs['messages'][0]['content']))
            return Stream()

    # Mock only the text-model transport, preserving classification parsing and
    # Gemini terminal routing together. No live API or device action is needed.
    monkeypatch.setattr(anthropic, 'AsyncAnthropic', Client)
    monkeypatch.setattr(response_outcome.app_config, 'REALTIME_SUMMARIZER_API_KEY', 'test')
    speech = _terminal(generation=generation)
    speech.server_content.output_transcription = SimpleNamespace(text=answer)
    agent = _agent([speech, _terminal()])
    agent._user_transcript = request
    events = _receive(agent)
    done = next(e for e in events if isinstance(e, TurnDoneEvent))
    assert captured == [{'request': request, 'spoken_answer': answer,
                         'public_search_evidence': False}]
    assert done.fallback_to_main is needs_main
    assert done.execution_completed is (not needs_main)
    assert done.user_transcript == request
    assert not _calls(events)
    _assert_done(agent, events)


def test_filler_confirmation_cannot_override_incomplete_check(monkeypatch):
    from hal.realtime import response_outcome

    async def check(*args, **kwargs):
        return False

    monkeypatch.setattr(response_outcome, 'spoken_response_complete', check)
    speech = _terminal()
    speech.server_content.output_transcription = SimpleNamespace(text='I can help with that.')
    agent = _agent([speech, _tool('complete_response')])
    events = _receive(agent)
    done = next(e for e in events if isinstance(e, TurnDoneEvent))
    assert done.fallback_to_main
    assert not done.execution_completed
    assert done.user_transcript == 'play a song'


def test_slow_outcome_check_is_cancelled_at_its_own_deadline(monkeypatch):
    from hal.realtime import response_outcome
    cancelled = []

    async def check(*args, **kwargs):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(True)

    monkeypatch.setattr(response_outcome, 'spoken_response_complete', check)
    speech = _terminal()
    speech.server_content.output_transcription = SimpleNamespace(text='I can help with that.')
    agent = _agent([speech])
    events = _receive(agent)
    assert cancelled == [True]
    assert next(e for e in events if isinstance(e, TurnDoneEvent)).fallback_to_main
