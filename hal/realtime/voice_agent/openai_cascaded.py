"""Cascaded voice brain — transcript in, text out, over any OpenAI-compatible /v1.

Drop-in replacement for `hal/pipecat_rt.py`: same public surface and the same
`TextChunk` / `ToolCall` events, without pipecat, its private asyncio loop or the
frame bridge. HAL keeps STT and TTS; this module owns only the middle of a turn:

    transcript ──▶ messages ──▶ LLM ──▶ text chunks
                                 └─ tools ──▶ web_search here, the rest to the host

Sync all the way: `run_turn` is a plain generator on the caller's thread, and a
host tool is answered during the yield that raised it.

    agent = CascadedAgent(CascadedConfig(), instructions=..., host_tools=[...])
    agent.start()
    for event in agent.run_turn("what time is it"):
        ...
    agent.stop()
"""

import base64
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Generator, Iterable

from hal.realtime.voice_agent.image_context import ImageContext

logger = logging.getLogger("hal.cascaded")

SEARCH_TOOL = "web_search"
UNANSWERED = '{"error": "host did not answer"}'


# --- Configuration ---------------------------------------------------------


@dataclass
class CascadedConfig:
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
    # Rounds of tool-call → result → inference within one turn.
    max_tool_rounds: int = int(os.environ.get("HAL_PIPECAT_MAX_ROUNDS", "4"))
    # In-session history kept in the message list. The durable memory lives in
    # the system prompt, which is rebuilt only on recycle — appending here keeps
    # the cached prefix intact, re-folding memory into the prompt would not.
    max_history_messages: int = int(os.environ.get("HAL_PIPECAT_MAX_HISTORY", "128"))
    # Images older than the K most recent are stripped to text-only (see
    # ImageContext) — a spoken session has no reason to keep every frame it
    # ever looked at live in the prompt, but the WORDS about them still matter.
    max_images: int = int(os.environ.get("HAL_PIPECAT_MAX_IMAGES", "3"))
    search_budget_per_turn: int = int(os.environ.get("HAL_PIPECAT_SEARCH_BUDGET", "2"))
    gemini_api_key: str = os.environ.get("HAL_PIPECAT_GEMINI_KEY", "")
    gemini_search_model: str = os.environ.get(
        "HAL_PIPECAT_GEMINI_SEARCH_MODEL", "gemini-3.5-flash-lite"
    )
    search_timeout_s: float = float(os.environ.get("HAL_PIPECAT_SEARCH_TIMEOUT_S", "6"))


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

    def reset(self, started: float) -> None:
        self.started = started
        self.request_sent = self.headers = self.first_token = 0.0
        self.prompt_chars = self.searches = 0
        self.prompt_tokens = self.completion_tokens = self.cached_tokens = 0
        self.reasoning_tokens = self.images = self.payload_bytes = self.requests = 0


# --- Agent -----------------------------------------------------------------


class CascadedAgent:
    """One cascaded voice-agent session.

    Args:
        config: endpoint, budgets and timeouts.
        instructions: the system prompt — the stable, cache-friendly prefix.
        host_tools: tools the HOST executes. Each is
            `(name, description, parameters_json_schema)`; calls are yielded as
            `ToolCall` and must be answered with `tool_result()` before the
            generator is resumed.
    """

    def __init__(
        self,
        config: CascadedConfig,
        instructions: str = "",
        host_tools: Iterable[tuple[str, str, dict]] = (),
    ) -> None:
        self._config = config
        self._instructions = instructions
        self._host_tools = list(host_tools)
        self._host_names = {name for name, _, _ in self._host_tools}
        self._messages: list[dict[str, Any]] = [
            {"role": "system", "content": instructions}
        ]
        self._images = ImageContext(config.max_images)
        self._tools = self._build_tools()
        self._client = None
        self._search_client = None
        self._turn = _TurnState()
        self._turn_lock = threading.Lock()
        self._results: dict[str, tuple[str, bool, Any]] = {}
        self._pending_images: list[dict] = []
        self._aborted = False
        self._stopping = False
        self.last_metrics: TurnMetrics | None = None

    # --- Lifecycle ---

    @property
    def available(self) -> bool:
        return self._client is not None and not self._stopping

    def start(self) -> bool:
        """Build the client and warm the prompt cache. False when unusable."""
        if not self._config.base_url or not self._config.model:
            logger.warning("[cascaded] base_url/model not configured — disabled")
            return False
        try:
            self._client = self._build_client()
        except Exception as e:
            logger.warning("[cascaded] client build failed: %s — disabled", e)
            return False
        self._stopping = False
        self._warm()
        return True

    def stop(self) -> None:
        self._stopping = True
        for client in (self._client, self._search_client):
            try:
                if client is not None:
                    client.close()
            except Exception:
                logger.debug("[cascaded] client close failed", exc_info=True)
        self._client = None
        self._search_client = None

    def _build_client(self):
        """OpenAI client with HTTP/2, retries and an on-the-wire byte counter.

        h2 measured 413 ms median TTFT against 642 ms, and the endpoint returns
        transient 500s that a retry absorbs.
        """
        import httpx
        from openai import DefaultHttpxClient, OpenAI

        def on_request(request) -> None:
            # The body as it actually goes on the wire — the only honest payload
            # number, since the SDK reshapes what the message list holds.
            try:
                self._turn.payload_bytes += len(request.content or b"")
                self._turn.requests += 1
            except Exception:
                pass

        limits = httpx.Limits(
            max_keepalive_connections=100, max_connections=1000, keepalive_expiry=None
        )
        hooks = {"request": [on_request]}
        try:
            http_client = DefaultHttpxClient(
                http2=self._config.http2, limits=limits, event_hooks=hooks
            )
        except ImportError:
            logger.warning("[cascaded] HTTP/2 needs httpx[http2]; using HTTP/1.1")
            http_client = DefaultHttpxClient(limits=limits, event_hooks=hooks)
        return OpenAI(
            api_key=self._config.api_key or "not-needed",
            base_url=self._config.base_url,
            http_client=http_client,
            max_retries=self._config.max_retries,
            # Backstop only; each request narrows this to what is left of the
            # turn deadline. Measured on device, a per-request timeout costs
            # +3 ms against the client default — bounding is free.
            timeout=self._config.turn_timeout_s,
        )

    def _warm(self) -> None:
        """Prefill the shared prefix once so the first real turn does not.

        Worth it here because the prompt is large (identity + skills + memory);
        on a small prefix it measures as noise. Time-boxed — a busy endpoint has
        blocked this for 13 s.
        """
        started = time.perf_counter()
        try:
            self._client.chat.completions.create(
                model=self._config.model,
                messages=self._messages + [{"role": "user", "content": "hi"}],
                max_tokens=2,
                stream=False,
                timeout=self._config.warm_timeout_s,
                **({"tools": self._tools} if self._tools else {}),
            )
            logger.info("[cascaded] context warmed in %.2fs", time.perf_counter() - started)
        except Exception as e:
            logger.warning("[cascaded] context warm-up skipped: %s", e)

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
            self._turn.reset(time.perf_counter())
            self._aborted = False
            self._results.clear()
            self._pending_images = []
            message = self._user_message(transcript, image)
            self._messages.append(message)
            deadline = time.monotonic() + self._config.turn_timeout_s
            try:
                yield from self._rounds(deadline)
            except Exception as e:
                logger.warning("[cascaded] turn failed: %s", e)
            finally:
                self.last_metrics = self._metrics()
                logger.info("[cascaded] %s", self.last_metrics)
                # Update the image window once the turn is actually over, not
                # when a frame was attached — a turn that never finished
                # streaming (aborted, exception) still keeps its image(s) live.
                # Covers both entry points: the transcript's own `image=` and
                # any a host tool (e.g. `look`) captured mid-turn.
                if image is not None:
                    self._images.track(message)
                for photo in self._pending_images:
                    self._images.track(photo)
                self._trim_history()

    def tool_result(
        self, call_id: str, output: str, run_llm: bool = True, image=None
    ) -> None:
        """Answer a ToolCall. `run_llm=False` ends the turn with that call.

        `image` (optional BGR frame) is how a vision tool (e.g. `look`) hands
        its capture back: appended as its own message right after the tool's
        text result, so the NEXT round of this same turn already sees it.
        """
        self._results[call_id] = (output, run_llm, image)

    def abort_turn(self) -> None:
        """Stop the turn at the next chunk boundary."""
        self._aborted = True

    def add_context(self, text: str) -> None:
        """Append a one-off context line (time, speaker) ahead of the next turn."""
        if text:
            self._messages.append({"role": "user", "content": text})

    def rebuild_context(self, instructions: str) -> None:
        """Recycle: swap the system prompt and drop in-session history.

        Cheap here — context is client-side, so there is no reconnect. Durable
        memory rides in `instructions`.
        """
        self._instructions = instructions
        self._messages = [{"role": "system", "content": instructions}]
        self._images.reset()

    def _rounds(self, deadline: float) -> Generator[Any, None, None]:
        """Stream, run whatever tools came back, repeat while the model owes words."""
        for _ in range(max(1, self._config.max_tool_rounds)):
            calls = yield from self._stream_once(deadline)
            if self._aborted or not calls:
                return
            run_again = False
            for call in calls:
                image = None
                if call["name"] == SEARCH_TOOL:
                    output, again = self._run_search(call["arguments"]), True
                elif call["name"] in self._host_names:
                    yield ToolCall(call["name"], call["arguments"], call["id"])
                    output, again, image = self._results.pop(
                        call["id"], (UNANSWERED, False, None)
                    )
                else:
                    logger.warning("[cascaded] unknown tool %r", call["name"])
                    output, again = '{"error": "unknown tool"}', True
                # Every raised call needs its result message, answered or not:
                # the endpoint rejects an assistant tool_calls block with a gap.
                self._messages.append(
                    {"role": "tool", "tool_call_id": call["id"], "content": output}
                )
                if image is not None:
                    # Appended now so THIS round's next request already carries
                    # it; tracked (K-window bookkeeping) only at turn end, same
                    # as the transcript's own `image=` — see run_turn.
                    photo = self._user_message("[Captured from the camera]", image)
                    self._messages.append(photo)
                    self._pending_images.append(photo)
                run_again = run_again or again
            if not run_again or self._aborted:
                return
            if time.monotonic() >= deadline:
                logger.warning("[cascaded] turn deadline reached before the next round")
                return
        logger.warning("[cascaded] tool-round cap reached — ending turn")

    def _stream_once(self, deadline: float) -> Generator[Any, None, list]:
        """One streamed request. Yields text, returns the tool calls it raised."""
        chars, images = _measure_messages(self._messages)
        self._turn.prompt_chars = chars
        self._turn.images = images
        # Timings describe the FIRST request of the turn: a tool round issues
        # another one, and mixing the two makes server/stream read negative.
        if not self._turn.request_sent:
            self._turn.request_sent = time.perf_counter()
        stream = self._client.chat.completions.create(
            model=self._config.model,
            messages=self._messages,
            max_tokens=self._config.max_tokens,
            temperature=self._config.temperature,
            stream=True,
            stream_options={"include_usage": True},
            # Narrows per round: without it a tool turn could spend the full
            # client timeout on EACH request rather than on the turn.
            timeout=max(1.0, deadline - time.monotonic()),
            **({"tools": self._tools} if self._tools else {}),
        )
        if not self._turn.headers:
            self._turn.headers = time.perf_counter()
        content: list[str] = []
        calls: dict[int, dict] = {}
        try:
            for chunk in stream:
                if self._aborted:
                    break
                if time.monotonic() >= deadline:
                    logger.warning("[cascaded] turn timed out mid-stream")
                    break
                self._absorb_usage(chunk)
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    if not self._turn.first_token:
                        self._turn.first_token = time.perf_counter()
                    content.append(delta.content)
                    yield TextChunk(delta.content)
                # Fragmented by construction: `id` and `name` land once,
                # `arguments` accumulates, and `index` keys them because a turn
                # may raise several calls at once.
                for tc in delta.tool_calls or ():
                    slot = calls.setdefault(
                        tc.index, {"id": "", "name": "", "arguments": ""}
                    )
                    slot["id"] = tc.id or slot["id"]
                    if tc.function is not None:
                        slot["name"] = tc.function.name or slot["name"]
                        slot["arguments"] += tc.function.arguments or ""
        finally:
            try:
                stream.close()
            except Exception:
                pass
        raised = [c for _, c in sorted(calls.items()) if c["name"] and c["id"]]
        self._append_assistant("".join(content), raised)
        return raised

    def _append_assistant(self, text: str, calls: list) -> None:
        if not text and not calls:
            return
        message: dict[str, Any] = {"role": "assistant", "content": text or None}
        if calls:
            message["tool_calls"] = [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {"name": c["name"], "arguments": c["arguments"] or "{}"},
                }
                for c in calls
            ]
        self._messages.append(message)

    def _user_message(self, transcript: str, image=None) -> dict[str, Any]:
        if image is None:
            return {"role": "user", "content": transcript}
        try:
            import cv2

            ok, buf = cv2.imencode(".jpg", image)
            if not ok:
                return {"role": "user", "content": transcript}
            url = "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()
            return {
                "role": "user",
                "content": [
                    {"type": "text", "text": transcript},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }
        except Exception as e:
            logger.warning("[cascaded] image attach failed: %s", e)
            return {"role": "user", "content": transcript}

    # --- Tools ---

    def _build_tools(self) -> list[dict]:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": params.get("properties", {}),
                        "required": params.get("required", []),
                    },
                },
            }
            for name, description, params in self._host_tools
        ]
        if self._config.gemini_api_key:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": SEARCH_TOOL,
                        "description": (
                            "Real-time access to current information on the web. Use it for "
                            "anything after your training cutoff, anything happening now, and "
                            "any fact you are not certain of — news, results, prices, weather, "
                            "schedules. You DO have this access: never reply that you cannot "
                            "provide real-time information."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "description": "What to search for.",
                                }
                            },
                            "required": ["query"],
                        },
                    },
                }
            )
        return tools

    def _run_search(self, arguments: str) -> str:
        """Gemini grounding search, capped per turn.

        The cap is not optional: uncapped, a model that cannot find a stated
        answer re-queries until it gives up — seconds of dead air per turn.
        """
        if self._turn.searches >= self._config.search_budget_per_turn:
            return json.dumps(
                {"error": "search budget for this turn is spent; answer with what you have"}
            )
        try:
            query = str(json.loads(arguments or "{}").get("query", ""))
        except (ValueError, TypeError):
            query = ""
        self._turn.searches += 1
        started = time.perf_counter()
        try:
            answer = self._gemini_search(query)
        except Exception as e:
            logger.warning("[cascaded] web_search failed: %s", e)
            return json.dumps({"error": "search unavailable"})
        logger.info(
            "[cascaded] web_search %.2fs query=%r -> %r",
            time.perf_counter() - started, query[:60], answer[:80],
        )
        return json.dumps({"answer": answer})

    def _gemini_search(self, query: str) -> str:
        import httpx

        if self._search_client is None:
            self._search_client = httpx.Client(timeout=self._config.search_timeout_s)
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
        response = self._search_client.post(
            url, json=payload, headers={"x-goog-api-key": self._config.gemini_api_key}
        )
        response.raise_for_status()
        parts = response.json()["candidates"][0]["content"]["parts"]
        return " ".join(p.get("text", "") for p in parts).strip()

    # --- Internals ---

    def _absorb_usage(self, chunk) -> None:
        """Accumulate: a turn with a tool call bills more than one request."""
        usage = getattr(chunk, "usage", None)
        if usage is None:
            return
        self._turn.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
        self._turn.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        self._turn.cached_tokens += getattr(prompt_details, "cached_tokens", 0) or 0
        out_details = getattr(usage, "completion_tokens_details", None)
        self._turn.reasoning_tokens += getattr(out_details, "reasoning_tokens", 0) or 0

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
        """Keep the system prompt, drop the oldest turns past the window.

        The window must never open on a tool result: its assistant `tool_calls`
        message would be gone and the endpoint rejects the orphan.
        """
        limit = self._config.max_history_messages
        if len(self._messages) > limit + 1:
            tail = self._messages[-limit:]
            while tail and tail[0].get("role") == "tool":
                tail.pop(0)
            self._messages = [self._messages[0]] + tail
        # A message-count trim can drop one still inside the K-image window —
        # without this the window undercounts by a phantom entry forever.
        self._images.reconcile(self._messages)


def _measure_messages(messages) -> tuple[int, int]:
    """(text chars, image parts) over a message list.

    Counts text separately from images: a base64 image part is ~100k characters
    and would otherwise swamp the prompt-size number it shares a field with.
    """
    chars = images = 0
    for message in messages:
        content = message.get("content") or ""
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


# --- Smoke test ------------------------------------------------------------
# python -m hal.realtime.voice_agent.openai_cascaded ["prompt" ...]
#   — needs HAL_PIPECAT_BASE_URL / HAL_PIPECAT_MODEL.

if __name__ == "__main__":
    import sys

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
    agent = CascadedAgent(
        CascadedConfig(),
        instructions=(
            "You are Lamp, a small desk robot. Keep replies to one or two short spoken "
            "sentences. When the user asks for device control, music, scheduling or "
            "memory, call delegate_to_main instead of answering."
        ),
        host_tools=[delegate],
    )
    started = time.perf_counter()
    if not agent.start():
        sys.exit("start failed — base_url/model unset, or the endpoint is unreachable")
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
