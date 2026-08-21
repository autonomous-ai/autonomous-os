"""Pipecat-based realtime voice agent — self-contained, cascaded (text in / text out).

Standalone by design: it shares nothing with `hal/realtime/` and imports none of
its provider machinery. ASR and TTS stay with the existing hal stack, so this
module only owns the middle of the turn:

    transcript ──▶ LLMContext ──▶ LLM (OpenAI-compatible) ──▶ text chunks
                                   └─ tools ──▶ handled here, or raised to the host

Pipecat runs on a private asyncio loop in a daemon thread; the public API is
sync and queue-based so a caller on hal's voice thread never touches the loop.

    agent = PipecatAgent(PipecatConfig(), instructions=..., host_tools=[...])
    agent.start()
    for event in agent.run_turn("what time is it"):
        ...
    agent.stop()

Requires `pipecat-ai[openai]` and `httpx[http2]`; neither is a hal dependency
yet. `start()` returns False when they are missing and `available` stays False.
"""

import asyncio
import json
import logging
import os
import queue
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Generator, Iterable

logger = logging.getLogger("hal.pipecat")

DONE = object()  # sentinel on the event queue: this turn produced its last frame


# --- Configuration ---------------------------------------------------------


@dataclass
class PipecatConfig:
    base_url: str = os.environ.get("HAL_PIPECAT_BASE_URL", "")
    api_key: str = os.environ.get("HAL_PIPECAT_API_KEY", "not-needed")
    model: str = os.environ.get("HAL_PIPECAT_MODEL", "")
    # Caps generated tokens, which INCLUDES tool-call arguments — a small budget
    # truncates the JSON and the turn is lost with a parse error, not a retry.
    max_tokens: int = int(os.environ.get("HAL_PIPECAT_MAX_TOKENS", "512"))
    temperature: float = float(os.environ.get("HAL_PIPECAT_TEMPERATURE", "0.6"))
    http2: bool = os.environ.get("HAL_PIPECAT_HTTP2", "true").lower() == "true"
    max_retries: int = int(os.environ.get("HAL_PIPECAT_MAX_RETRIES", "2"))
    warm_timeout_s: float = float(os.environ.get("HAL_PIPECAT_WARM_TIMEOUT_S", "5"))
    turn_timeout_s: float = float(os.environ.get("HAL_PIPECAT_TURN_TIMEOUT_S", "20"))
    tool_result_timeout_s: float = float(os.environ.get("HAL_PIPECAT_TOOL_TIMEOUT_S", "4"))
    # In-session history kept in the message list. The durable memory lives in
    # the system prompt, which is rebuilt only on recycle — appending here keeps
    # the cached prefix intact, re-folding memory into the prompt would not.
    max_history_messages: int = int(os.environ.get("HAL_PIPECAT_MAX_HISTORY", "128"))
    search_budget_per_turn: int = int(os.environ.get("HAL_PIPECAT_SEARCH_BUDGET", "2"))
    gemini_api_key: str = os.environ.get("HAL_PIPECAT_GEMINI_KEY", "")
    gemini_search_model: str = os.environ.get(
        "HAL_PIPECAT_GEMINI_SEARCH_MODEL", "gemini-3.5-flash-lite"
    )
    search_timeout_s: float = float(os.environ.get("HAL_PIPECAT_SEARCH_TIMEOUT_S", "6"))
    # pipecat logs through loguru at DEBUG, which floods the device journal with
    # per-frame lines. Raise to DEBUG only when tracing the pipeline.
    log_level: str = os.environ.get("HAL_PIPECAT_LOG_LEVEL", "WARNING")


# --- Turn events -----------------------------------------------------------


@dataclass
class TextChunk:
    text: str


@dataclass
class ToolCall:
    """A tool the host must run (device control, delegation, camera)."""

    name: str
    arguments: str
    call_id: str


@dataclass
class TurnMetrics:
    """Where a turn's latency went and what it cost.

    A turn that calls a tool makes more than one request, so token counts and
    payload bytes are TURN TOTALS across `requests`, not per-request values.
    """

    wait: float = 0.0
    server: float = 0.0
    stream: float = 0.0
    ttft: float = 0.0
    prompt_chars: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    images: int = 0
    payload_bytes: int = 0
    requests: int = 0

    def __str__(self) -> str:
        parts = [
            f"ttft {self.ttft:.2f}s",
            f"wait {self.wait:.2f}s",
            f"server {self.server:.2f}s",
            f"stream {self.stream:.2f}s",
        ]
        prompt = f"prompt {self.prompt_chars / 1000:.1f}k chars"
        if self.prompt_tokens:
            prompt += f"/{self.prompt_tokens} tok"
            if self.cached_tokens:
                pct = 100.0 * self.cached_tokens / self.prompt_tokens
                prompt += f" ({self.cached_tokens} cached, {pct:.0f}%)"
        parts.append(prompt)
        if self.completion_tokens or self.reasoning_tokens:
            out = f"out {self.completion_tokens} tok"
            if self.reasoning_tokens:
                out += f" ({self.reasoning_tokens} reasoning)"
            parts.append(out)
        if self.images:
            parts.append(f"images {self.images}")
        if self.payload_bytes:
            parts.append(f"payload {self.payload_bytes / 1024:.1f} KB")
        if self.requests > 1:
            parts.append(f"{self.requests} requests")
        return " | ".join(parts)


@dataclass
class _TurnState:
    """Per-turn scratch space.

    Reset in place, never replaced: the LLM service and the collector capture
    this object when the pipeline is built, so a fresh instance would strand
    their writes on an orphan and the turn would never see its own end frame.
    """

    started: float = 0.0
    request_sent: float = 0.0
    headers: float = 0.0
    first_token: float = 0.0
    prompt_chars: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    images: int = 0
    payload_bytes: int = 0
    requests: int = 0
    searches: int = 0
    pending: dict = field(default_factory=dict)
    calls_started: int = 0
    response_ended: bool = False

    def reset(self, started: float) -> None:
        self.started = started
        self.request_sent = self.headers = self.first_token = 0.0
        self.prompt_chars = self.searches = self.calls_started = 0
        self.prompt_tokens = self.completion_tokens = self.cached_tokens = 0
        self.reasoning_tokens = self.images = self.payload_bytes = self.requests = 0
        self.response_ended = False
        self.pending.clear()


# --- Agent -----------------------------------------------------------------


class PipecatAgent:
    """One cascaded voice-agent session.

    Args:
        config: endpoint, budgets and timeouts.
        instructions: the system prompt — the stable, cache-friendly prefix.
        host_tools: tools the HOST executes. Each is
            `(name, description, parameters_json_schema)`; calls are yielded as
            `ToolCall` and must be answered with `tool_result()`.
    """

    def __init__(
        self,
        config: PipecatConfig,
        instructions: str = "",
        host_tools: Iterable[tuple[str, str, dict]] = (),
    ) -> None:
        self._config = config
        self._instructions = instructions
        self._host_tools = list(host_tools)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._worker = None
        self._context = None
        self._llm = None
        self._events: queue.Queue = queue.Queue()
        self._turn = _TurnState()
        self._turn_lock = threading.Lock()
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._session_id = str(uuid.uuid4())
        self._search_client = None
        self.last_metrics: TurnMetrics | None = None

    # --- Lifecycle ---

    @property
    def available(self) -> bool:
        return self._ready.is_set() and not self._stopping.is_set()

    def start(self) -> bool:
        """Build the pipeline and warm the prompt cache. False when unusable."""
        if not self._config.base_url or not self._config.model:
            logger.warning("[pipecat] base_url/model not configured — disabled")
            return False
        try:
            import pipecat  # noqa: F401
        except ImportError as e:
            logger.warning("[pipecat] not installed (%s) — disabled", e)
            return False
        try:
            from loguru import logger as _loguru

            _loguru.remove()
            _loguru.add(sys.stderr, level=self._config.log_level)
        except Exception:
            pass

        self._stopping.clear()
        started = threading.Event()
        self._thread = threading.Thread(
            target=self._run_loop, args=(started,), daemon=True, name="pipecat-loop"
        )
        self._thread.start()
        started.wait(timeout=30)
        return self.available

    def stop(self) -> None:
        self._stopping.set()
        self._ready.clear()
        loop = self._loop
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), loop).result(timeout=5)
        except Exception:
            logger.exception("[pipecat] shutdown failed")
        # Cancel pipecat's own tasks and let them unwind before stopping the
        # loop; tearing it down under them logs a wall of "Task was destroyed"
        # and leaks whatever they were holding.
        try:
            asyncio.run_coroutine_threadsafe(_cancel_tasks(), loop).result(timeout=5)
        except Exception:
            logger.debug("[pipecat] task cancellation incomplete")
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run_loop(self, started: threading.Event) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._build())
            self._ready.set()
        except Exception:
            logger.exception("[pipecat] pipeline build failed")
        finally:
            started.set()
        try:
            loop.run_forever()
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    async def _build(self) -> None:
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.worker import PipelineParams, PipelineWorker
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMContextAggregatorPair,
        )
        from pipecat.workers.runner import WorkerRunner

        self._llm = _build_llm(self._config, self._turn)
        self._context = LLMContext(
            [{"role": "system", "content": self._instructions}],
            tools=self._build_tools(),
        )
        aggregators = LLMContextAggregatorPair(self._context)
        self._worker = PipelineWorker(
            Pipeline(
                [
                    aggregators.user(),
                    self._llm,
                    _Collector(self._events, self._turn),
                    aggregators.assistant(),
                ]
            ),
            params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
            idle_timeout_secs=None,
            conversation_id=self._session_id,
        )
        asyncio.create_task(WorkerRunner(handle_sigint=False).run(self._worker))
        await self._warm()

    async def _shutdown(self) -> None:
        from pipecat.frames.frames import EndFrame

        if self._worker is not None:
            await self._worker.queue_frame(EndFrame())
        if self._search_client is not None:
            await self._search_client.aclose()
            self._search_client = None

    # --- Turns ---

    def run_turn(self, transcript: str, image=None) -> Generator[Any, None, None]:
        """Run one turn, yielding TextChunk / ToolCall until the model is done.

        Args:
            transcript: the user's final STT text — this agent never sees audio.
            image: optional BGR frame added to this turn only.
        """
        if not self.available:
            return
        with self._turn_lock:
            self._drain()
            self._turn.reset(time.perf_counter())
            deadline = time.monotonic() + self._config.turn_timeout_s
            self._submit(self._queue_turn(transcript, image))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning("[pipecat] turn timed out with no completion")
                    break
                try:
                    event = self._events.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    continue
                if event is DONE:
                    break
                yield event
            self.last_metrics = self._metrics()
            logger.info("[pipecat] %s", self.last_metrics)
            self._trim_history()

    def tool_result(self, call_id: str, output: str, run_llm: bool = True) -> None:
        """Answer a ToolCall. `run_llm=False` records it without a new inference."""
        future = self._turn.pending.pop(call_id, None)
        if future is None or self._loop is None:
            return
        self._loop.call_soon_threadsafe(
            lambda: future.done() or future.set_result((output, run_llm))
        )

    def _settle_call(self, run_llm: bool) -> None:
        """Close the turn once a tool call has been answered, if nothing follows.

        The response's end frame arrives while the handler is still waiting on
        the host, so the decision can only be made here: a result that triggers
        a new inference means more output is coming; one that does not means
        the turn ended with that call. Runs on the loop thread, same as the
        collector, so the counters need no lock.
        """
        turn = self._turn
        turn.calls_started = max(0, turn.calls_started - 1)
        if run_llm:
            turn.response_ended = False
        elif turn.calls_started == 0 and turn.response_ended:
            turn.response_ended = False
            self._events.put(DONE)

    def abort_turn(self) -> None:
        """Release anything awaiting a tool result and end the turn now."""
        for call_id in list(self._turn.pending):
            self.tool_result(call_id, '{"result": "aborted"}', run_llm=False)
        self._events.put(DONE)

    def add_context(self, text: str) -> None:
        """Append a one-off context line (time, speaker) ahead of the next turn."""
        if self._context is not None and text:
            self._context.add_message({"role": "user", "content": text})

    def rebuild_context(self, instructions: str) -> None:
        """Recycle: swap the system prompt and drop in-session history.

        Cheap here — context is client-side, so there is no reconnect. Durable
        memory rides in `instructions`.
        """
        self._instructions = instructions
        if self._context is not None:
            self._context.set_messages([{"role": "system", "content": instructions}])

    async def _queue_turn(self, transcript: str, image) -> None:
        from pipecat.frames.frames import TranscriptionFrame

        if image is not None:
            self._add_image(image)
        await self._worker.queue_frame(
            TranscriptionFrame(transcript, "user", str(time.time()))
        )

    def _add_image(self, image) -> None:
        """Write the frame straight into the context.

        Deliberately not a UserImageRawFrame: the aggregator answers that with
        its own inference, which makes every turn reply twice.
        """
        try:
            import cv2

            ok, buf = cv2.imencode(".jpg", image)
            if ok:
                self._context.add_image_frame_message(
                    format="JPEG", size=(image.shape[1], image.shape[0]),
                    image=buf.tobytes(),
                )
        except Exception as e:
            logger.warning("[pipecat] image attach failed: %s", e)

    # --- Tools ---

    def _build_tools(self):
        from pipecat.adapters.schemas.function_schema import FunctionSchema
        from pipecat.adapters.schemas.tools_schema import ToolsSchema

        schemas = [
            FunctionSchema(
                name=name,
                description=description,
                properties=params.get("properties", {}),
                required=params.get("required", []),
                handler=self._host_handler(name),
            )
            for name, description, params in self._host_tools
        ]
        if self._config.gemini_api_key:
            schemas.append(
                FunctionSchema(
                    name="web_search",
                    description=(
                        "Real-time access to current information on the web. Use it for "
                        "anything after your training cutoff, anything happening now, and "
                        "any fact you are not certain of — news, results, prices, weather, "
                        "schedules. You DO have this access: never reply that you cannot "
                        "provide real-time information."
                    ),
                    properties={"query": {"type": "string", "description": "What to search for."}},
                    required=["query"],
                    handler=self._search_handler,
                )
            )
        return ToolsSchema(standard_tools=schemas) if schemas else None

    def _host_handler(self, name: str) -> Callable:
        """Raise the call to the host and wait for `tool_result()`."""

        async def handler(params) -> None:
            from pipecat.frames.frames import FunctionCallResultProperties

            future = asyncio.get_running_loop().create_future()
            self._turn.pending[params.tool_call_id] = future
            self._events.put(
                ToolCall(
                    name=name,
                    arguments=json.dumps(params.arguments or {}),
                    call_id=params.tool_call_id,
                )
            )
            try:
                output, run_llm = await asyncio.wait_for(
                    future, timeout=self._config.tool_result_timeout_s
                )
            except asyncio.TimeoutError:
                self._turn.pending.pop(params.tool_call_id, None)
                output, run_llm = '{"error": "host did not answer"}', False
            await params.result_callback(
                output, properties=FunctionCallResultProperties(run_llm=run_llm)
            )
            self._settle_call(run_llm)

        return handler

    async def _search_handler(self, params) -> None:
        """Gemini grounding search, capped per turn.

        The cap is not optional: uncapped, a model that cannot find a stated
        answer re-queries until it gives up — seconds of dead air per turn.
        """
        query = (params.arguments or {}).get("query", "")
        if self._turn.searches >= self._config.search_budget_per_turn:
            await params.result_callback(
                {"error": "search budget for this turn is spent; answer with what you have"}
            )
            self._settle_call(True)
            return
        self._turn.searches += 1
        started = time.perf_counter()
        try:
            answer = await self._gemini_search(query)
        except Exception as e:
            logger.warning("[pipecat] web_search failed: %s", e)
            await params.result_callback({"error": "search unavailable"})
            self._settle_call(True)
            return
        logger.info(
            "[pipecat] web_search %.2fs query=%r -> %r",
            time.perf_counter() - started, query[:60], answer[:80],
        )
        await params.result_callback({"answer": answer})
        self._settle_call(True)

    async def _gemini_search(self, query: str) -> str:
        import httpx

        if self._search_client is None:
            self._search_client = httpx.AsyncClient(timeout=self._config.search_timeout_s)
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self._config.gemini_search_model}:generateContent"
        )
        payload = {
            "contents": [{"role": "user", "parts": [{"text": query}]}],
            "tools": [{"google_search": {}}],
            "systemInstruction": {
                "parts": [{
                    "text": "Answer in ONE short factual sentence. No markdown, no lists, "
                            "no citations, no preamble. If unknown, say so in one sentence.",
                }]
            },
            "generationConfig": {"maxOutputTokens": 120, "thinkingLevel": "minimal"},
        }
        response = await self._search_client.post(
            url, json=payload, headers={"x-goog-api-key": self._config.gemini_api_key}
        )
        response.raise_for_status()
        parts = response.json()["candidates"][0]["content"]["parts"]
        return " ".join(p.get("text", "") for p in parts).strip()

    # --- Internals ---

    def _submit(self, coro) -> None:
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _drain(self) -> None:
        """Clear anything a previous turn left behind, so a turn reads only its own."""
        dropped = 0
        while True:
            try:
                self._events.get_nowait()
            except queue.Empty:
                break
            dropped += 1
        if dropped:
            logger.info("[pipecat] dropped %d stale event(s)", dropped)

    def _metrics(self) -> TurnMetrics:
        t = self._turn
        return TurnMetrics(
            wait=(t.request_sent - t.started) if t.request_sent else 0.0,
            server=(t.headers - t.request_sent) if t.headers and t.request_sent else 0.0,
            stream=(t.first_token - t.headers) if t.first_token and t.headers else 0.0,
            ttft=(t.first_token - t.started) if t.first_token else 0.0,
            prompt_chars=t.prompt_chars,
            prompt_tokens=t.prompt_tokens,
            completion_tokens=t.completion_tokens,
            cached_tokens=t.cached_tokens,
            reasoning_tokens=t.reasoning_tokens,
            images=t.images,
            payload_bytes=t.payload_bytes,
            requests=t.requests,
        )

    def _trim_history(self) -> None:
        """Keep the system prompt, drop the oldest turns past the window."""
        if self._context is None:
            return
        messages = self._context.get_messages()
        limit = self._config.max_history_messages
        if len(messages) <= limit + 1:
            return
        self._context.set_messages([messages[0]] + messages[-limit:])

    async def _warm(self) -> None:
        """Prefill the shared prefix once so the first real turn does not.

        Worth it here because the prompt is large (identity + skills + memory);
        on a small prefix it measures as noise. Time-boxed — a busy endpoint has
        blocked this for 13 s.
        """
        try:
            invocation = self._llm.get_llm_adapter().get_llm_invocation_params(
                self._context, convert_developer_to_user=True
            )
            params = {
                "model": self._config.model,
                "messages": self._context.get_messages() + [{"role": "user", "content": "hi"}],
                "max_tokens": 2,
                "stream": False,
            }
            if invocation.get("tools"):
                params["tools"] = invocation["tools"]
            started = time.perf_counter()
            await asyncio.wait_for(
                self._llm._client.chat.completions.create(**params),
                timeout=self._config.warm_timeout_s,
            )
            logger.info("[pipecat] context warmed in %.2fs", time.perf_counter() - started)
        except asyncio.TimeoutError:
            logger.warning("[pipecat] context warm-up timed out — starting anyway")
        except Exception as e:
            logger.warning("[pipecat] context warm-up skipped: %s", e)


# --- Pipeline pieces -------------------------------------------------------


async def _cancel_tasks() -> None:
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _measure_messages(messages) -> tuple[int, int]:
    """(text chars, image parts) over a message list.

    Counts text separately from images: a base64 image part is ~100k characters
    and would otherwise swamp the prompt-size number it shares a field with.
    """
    chars = images = 0
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, str):
            chars += len(content)
            continue
        for part in content or ():
            if not isinstance(part, dict):
                chars += len(str(part))
            elif part.get("type") == "text":
                chars += len(part.get("text", ""))
            else:
                images += 1
    return chars, images


def _build_llm(config: PipecatConfig, turn: _TurnState):
    from pipecat.services.openai.llm import OpenAILLMService

    class _TimedLLM(OpenAILLMService):
        """Adds HTTP/2, retries and request timing to pipecat's OpenAI client.

        Pipecat builds the client HTTP/1.1-only with retries off; h2 measured
        413 ms median TTFT against 642 ms, and the endpoint returns transient
        500s that a retry absorbs.
        """

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._turn = turn

        def create_client(self, api_key=None, base_url=None, **kwargs):
            import httpx
            from openai import AsyncOpenAI, DefaultAsyncHttpxClient

            limits = httpx.Limits(
                max_keepalive_connections=100, max_connections=1000, keepalive_expiry=None
            )
            async def on_request(request) -> None:
                # The body as it actually goes on the wire — the only honest
                # payload number, since the SDK reshapes what the context holds.
                try:
                    turn.payload_bytes += len(request.content or b"")
                    turn.requests += 1
                except Exception:
                    pass

            hooks = {"request": [on_request]}
            try:
                http_client = DefaultAsyncHttpxClient(
                    http2=config.http2, limits=limits, event_hooks=hooks
                )
            except ImportError:
                logger.warning('[pipecat] HTTP/2 needs httpx[http2]; using HTTP/1.1')
                http_client = DefaultAsyncHttpxClient(limits=limits, event_hooks=hooks)
            client = AsyncOpenAI(
                api_key=api_key, base_url=base_url, http_client=http_client
            )
            return client.with_options(max_retries=config.max_retries)

        async def get_chat_completions(self, context):
            chars, images = _measure_messages(context.get_messages())
            self._turn.prompt_chars = chars
            self._turn.images = images
            self._turn.request_sent = time.perf_counter()
            stream = await super().get_chat_completions(context)
            self._turn.headers = time.perf_counter()
            return stream

        async def start_llm_usage_metrics(self, tokens):
            """Accumulate: a turn with a tool call bills more than one request."""
            self._turn.prompt_tokens += tokens.prompt_tokens or 0
            self._turn.completion_tokens += tokens.completion_tokens or 0
            self._turn.cached_tokens += getattr(tokens, "cache_read_input_tokens", 0) or 0
            self._turn.reasoning_tokens += getattr(tokens, "reasoning_tokens", 0) or 0
            await super().start_llm_usage_metrics(tokens)

    return _TimedLLM(
        api_key=config.api_key,
        base_url=config.base_url,
        settings=OpenAILLMService.Settings(
            model=config.model,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
        ),
    )


class _Collector:
    """Bridges pipeline frames onto the sync event queue."""

    def __new__(cls, events: queue.Queue, turn: _TurnState):
        from pipecat.frames.frames import (
            ErrorFrame,
            FunctionCallsStartedFrame,
            LLMFullResponseEndFrame,
            TextFrame,
        )
        from pipecat.processors.frame_processor import FrameProcessor

        class Collector(FrameProcessor):
            async def process_frame(self, frame, direction):
                await super().process_frame(frame, direction)
                if isinstance(frame, TextFrame) and frame.text:
                    if not turn.first_token:
                        turn.first_token = time.perf_counter()
                    events.put(TextChunk(frame.text))
                elif isinstance(frame, FunctionCallsStartedFrame):
                    turn.calls_started += len(frame.function_calls or [None])
                elif isinstance(frame, ErrorFrame):
                    logger.warning("[pipecat] %s", frame)
                    events.put(DONE)
                elif isinstance(frame, LLMFullResponseEndFrame):
                    # A response that raised tool calls is not the end of the
                    # turn — the results decide. See PipecatAgent._settle_call.
                    if turn.calls_started:
                        turn.response_ended = True
                    else:
                        events.put(DONE)
                await self.push_frame(frame, direction)

        return Collector()


# --- Smoke test ------------------------------------------------------------
# python -m hal.pipecat_rt ["prompt" ...]   — needs HAL_PIPECAT_BASE_URL/_MODEL.

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    prompts = sys.argv[1:] or [
        "hello, who are you?",
        "what is the capital of Vietnam?",
        "play some jazz music please",
    ]
    delegate = (
        "delegate_to_main",
        "Hand the request to the main system: device control, music, scheduling, "
        "memory, skills. Pass a short summary of what the user wants.",
        {
            "properties": {
                "message": {"type": "string", "description": "What the user actually asked for."}
            },
            "required": ["message"],
        },
    )
    agent = PipecatAgent(
        PipecatConfig(),
        instructions=(
            "You are Lamp, a small desk robot. Keep replies to one or two short spoken "
            "sentences. When the user asks for device control, music, scheduling or "
            "memory, call delegate_to_main instead of answering."
        ),
        host_tools=[delegate],
    )
    started = time.perf_counter()
    if not agent.start():
        sys.exit("start failed — missing pipecat-ai/httpx[http2], or base_url/model unset")
    print(f"ready in {time.perf_counter() - started:.2f}s")

    for prompt in prompts:
        print(f"\nuser: {prompt}")
        reply = ""
        for event in agent.run_turn(prompt):
            if isinstance(event, TextChunk):
                reply += event.text
            elif isinstance(event, ToolCall):
                print(f"  tool {event.name}({event.arguments})")
                agent.tool_result(event.call_id, '{"result": "delegated"}', run_llm=False)
        print(f"lamp: {reply.strip()!r}\n  {agent.last_metrics}")

    agent.stop()
