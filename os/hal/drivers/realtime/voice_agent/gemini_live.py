"""Gemini Live voice agent implementation — queue-based threading."""

import asyncio
import json
import logging
import queue
import threading
import time
from contextlib import AsyncExitStack
from typing import Any, override

import cv2
import google.genai as genai
import numpy as np
import numpy.typing as npt
from google.genai import errors as genai_errors
from google.genai import types
from google.genai.live import AsyncSession
from websockets.exceptions import ConnectionClosed

from hal.drivers.realtime.config import GeminiConfig
from hal.drivers.realtime.models import (
    AgentInputEvent,
    AudioCommitEvent,
    AudioInput,
    AudioOutput,
    FunctionCallOutput,
    FunctionCallResultInput,
    ImageInput,
    InputBase,
    InputEvent,
    OutputEvent,
    TextInput,
    TextOutput,
    TurnDoneEvent,
)
from hal.drivers.realtime.utils import float32_to_pcm16_bytes, pcm16_bytes_to_float32
from hal.drivers.realtime.voice_agent.base import VoiceAgentBase

logger = logging.getLogger(__name__)
# Per-turn token/cost lines go to their own file (gemini_usage.log) via a
# dedicated logger configured in server_support/log_setup.py (propagate=False),
# so they don't mix into server.log.
usage_logger = logging.getLogger("hal.realtime.usage")

# Gemini Live pricing, USD per 1M tokens, keyed (direction, modality), PER MODEL.
# Source: ai.google.dev/gemini-api/docs/pricing (verified 2026-06-29). Audio rates
# happen to match across these models ($3 in / $12 out) — only text differs — but
# the table is explicit per model so adding a new model is one entry and a wrong
# audio rate can't hide behind a shared default. Keys match as a SUBSTRING of
# self._config.model so a dated preview suffix (…-preview-12-2025) still resolves.
_GEMINI_RATES: dict[str, dict[tuple[str, str], float]] = {
    "gemini-2.5-flash-native-audio": {
        ("in", "TEXT"): 0.50, ("in", "AUDIO"): 3.0,
        ("out", "TEXT"): 2.0, ("out", "AUDIO"): 12.0,
    },
    "gemini-3.1-flash-live": {
        ("in", "TEXT"): 0.75, ("in", "AUDIO"): 3.0,
        ("out", "TEXT"): 4.5, ("out", "AUDIO"): 12.0,
    },
}
# Unknown model → fall back to the entry with the highest text-out rate (the
# dominant text cost), so a future/untabled model logs a cost CEILING rather than
# an under-report. Avoids the old `"native-audio" in model` heuristic mis-pricing
# a hypothetical 3.x native-audio model at 2.5 rates.
_GEMINI_RATES_FALLBACK: dict[tuple[str, str], float] = max(
    _GEMINI_RATES.values(), key=lambda r: r[("out", "TEXT")]
)


def _gemini_rates_for(model: str) -> dict[tuple[str, str], float]:
    """Resolve the per-1M-token rate table for a model name (substring match);
    unknown models fall back to the most expensive table (cost = ceiling)."""
    for key, table in _GEMINI_RATES.items():
        if key in model:
            return table
    return _GEMINI_RATES_FALLBACK


class GeminiLiveAgent(VoiceAgentBase):
    def __init__(
        self,
        config: GeminiConfig,
        tools: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(tools=tools)
        self._config: GeminiConfig = config
        client_kwargs: dict = {"api_key": config.api_key}
        if config.base_url:
            client_kwargs["http_options"] = types.HttpOptions(base_url=config.base_url)
        self._client: genai.Client = genai.Client(**client_kwargs)
        self._session: AsyncSession | None = None
        self._exit_stack: AsyncExitStack | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._io_thread: threading.Thread | None = None
        self._resumption_handle: str | None = None
        self._speech_ended_at: float | None = None
        self._first_audio_received: bool = False
        self._vad_disabled: bool = not config.vad_enabled
        self._activity_started: bool = False
        self._reconnect_delay_s: float = config.reconnect_delay_s
        self._last_reconnect_at: float = 0.0
        # Exponential backoff: the recv loop self-reconnects when disconnected, so a
        # persistent failure (e.g. Gemini usage-limit 4029) would otherwise hammer
        # the endpoint every reconnect_delay_s. Grow the throttle on each failed
        # reconnect (capped), reset to the base on success.
        self._reconnect_backoff: float = config.reconnect_delay_s
        self._reconnect_backoff_max: float = 60.0
        self._max_retries: int = config.max_retries
        self._send_timeout_s: float = config.send_timeout_s
        self._recv_timeout_s: float = config.recv_timeout_s
        self._queue_poll_s: float = config.queue_poll_s
        self._join_timeout_s: float = config.join_timeout_s
        # Signals that the model is idle (no active turn). Set by default,
        # cleared when activityEnd is sent, set again on turn_complete.
        self._turn_done: threading.Event = threading.Event()
        self._turn_done.set()

    @property
    @override
    def sample_rate(self) -> int:
        return self._config.sample_rate

    @property
    def output_sample_rate(self) -> int:
        # Gemini Live always streams native audio output at 24 kHz, regardless of
        # the 16 kHz input rate (`sample_rate`).
        return 24000

    def _build_config(self) -> types.LiveConnectConfig:
        lang: str | None = self._config.language
        lang_codes: list[str] | None = (
            [lang] if lang and self._config.use_language_codes else None
        )

        live_config: types.LiveConnectConfig = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=self._config.voice.value,
                    )
                ),
                # Native-audio models (e.g. gemini-2.5-flash-native-audio) REJECT an
                # explicit language_code (server closes setup with WS 1000) — they
                # auto-detect the language. Only half-cascade / 3.x Live accept it.
                # The system prompt already enforces the spoken language either way.
                # Verified via on-device bisect: removing speech_config.language_code
                # is the only change that lets 2.5 connect.
                language_code=(
                    None if "native-audio" in self._config.model else lang
                ),
            ),
            system_instruction=self._config.instructions,
            input_audio_transcription=None,
            output_audio_transcription=types.AudioTranscriptionConfig(
                language_codes=lang_codes,
            ),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=self._vad_disabled,
                ),
            ),
            thinking_config=types.ThinkingConfig(
                thinking_level=self._config.thinking_level.value,
                # Don't return thought summaries to the client — otherwise the
                # model's reasoning leaks into model_turn parts and gets spoken /
                # transcribed. We still filter thought parts on parse (below) as a
                # second line of defense.
                include_thoughts=False,
            ),
            # NO context_window_compression. DO NOT re-add it while running behind the
            # campaign-api proxy: when compression fires the Gemini server performs it
            # via a session-resumption handoff (sessionResumptionUpdate + CLOSE 1000),
            # and the proxy does not support resumption, so the in-flight turn dies
            # mid-answer. Cost is controlled by shrinking the per-turn floor (memory
            # caps + skills-catalog trim) + session recycle instead. Only reconsider
            # against a resumption-capable endpoint (direct Google base_url).
        )

        live_tools: list[types.Tool] = []
        if self._tools:
            declarations: list[types.FunctionDeclaration] = [
                types.FunctionDeclaration(
                    name=tool["name"],
                    description=tool.get("description", ""),
                    parameters=tool.get("parameters"),
                )
                for tool in self._tools
            ]
            live_tools.append(types.Tool(function_declarations=declarations))
        # Google Search grounding is a separate built-in Tool (no function
        # declaration). Gemini decides on its own when to ground; results are
        # synthesized into the spoken reply, so the user never sees raw search
        # output. This lets weather/news/live-lookup turns resolve in-session
        # instead of delegating to main.
        if self._config.google_search_enabled:
            live_tools.append(types.Tool(google_search=types.GoogleSearch()))
        if live_tools:
            live_config.tools = live_tools

        # Session resumption (OPT-IN, default off). When enabled, the FIRST connect
        # sends handle=None to make the server start emitting resumption handles;
        # a reconnect then passes the latest handle to resume the SAME session
        # (context preserved). This only works if the WS endpoint faithfully
        # forwards the resumption handshake — the `campaign-api` proxy does NOT, and
        # resuming through it produces a zombie session (connected, accepts audio,
        # never responds). Cold reconnects work through the proxy, so resumption
        # stays gated behind HAL_GEMINI_SESSION_RESUMPTION. Enable only against an
        # endpoint that supports it (e.g. a direct Google base_url).
        if self._config.session_resumption_enabled:
            live_config.session_resumption = types.SessionResumptionConfig(
                handle=self._resumption_handle,
            )

        return live_config

    # --- Async internals (run on private event loop) ---

    async def _async_connect(self) -> None:
        logger.info(
            "Connecting to Gemini Live API (base_url=%s, model=%s)",
            self._config.base_url,
            self._config.model,
        )
        self._exit_stack = AsyncExitStack()
        self._session = await self._exit_stack.enter_async_context(
            self._client.aio.live.connect(
                model=self._config.model,
                config=self._build_config(),
            )
        )
        logger.info("[realtime] Gemini Live session open (voice=%s)", self._config.voice)

    async def _async_disconnect(self) -> None:
        if self._exit_stack is not None:
            logger.info("[realtime] Disconnecting from Gemini Live API")
            await self._exit_stack.aclose()
            self._exit_stack = None
            self._session = None

    async def _async_send_input(self, input: InputBase | None) -> None:
        if self._session is None or input is None:
            return
        if isinstance(input, AudioInput):
            # When VAD is disabled, send activityStart before first audio
            if self._vad_disabled and not self._activity_started:
                await self._session.send_realtime_input(
                    activity_start=types.ActivityStart()
                )
                self._activity_started = True
                logger.debug("[realtime] Sent activityStart (manual VAD)")

            self._speech_ended_at = time.monotonic()
            pcm_bytes: bytes = float32_to_pcm16_bytes(input.audio)
            await self._session.send_realtime_input(
                audio=types.Blob(
                    data=pcm_bytes,
                    mime_type=f"audio/pcm;rate={self._config.sample_rate}",
                )
            )
        elif isinstance(input, TextInput):
            await self._session.send_client_content(
                turns=types.Content(
                    parts=[types.Part(text=input.text)],
                    role="user",
                ),
                turn_complete=False,
            )
        elif isinstance(input, ImageInput):
            _: bool
            buf: npt.NDArray[np.uint8]
            _, buf = cv2.imencode(".jpg", input.image)
            await self._session.send_realtime_input(
                video=types.Blob(data=buf.tobytes(), mime_type="image/jpeg")
            )
        elif isinstance(input, FunctionCallResultInput):
            # input.output is normally a JSON object string, but send_function_result()
            # is public and may be handed arbitrary text — Gemini's response field
            # requires a dict, so coerce non-object/invalid payloads instead of
            # letting json.loads crash the IO loop.
            try:
                parsed: Any = json.loads(input.output)
            except (json.JSONDecodeError, TypeError):
                parsed = {"result": input.output}
            if not isinstance(parsed, dict):
                parsed = {"result": parsed}
            await self._session.send_tool_response(
                function_responses=[
                    types.FunctionResponse(
                        id=input.call_id,
                        response=parsed,
                    )
                ]
            )

    async def _async_commit(self) -> None:
        if self._session is None:
            return
        if self._vad_disabled and self._activity_started:
            await self._session.send_realtime_input(activity_end=types.ActivityEnd())
            self._activity_started = False
            self._turn_done.clear()
            logger.debug("[realtime] Sent activityEnd (manual VAD)")

    async def _async_receive_turn(self) -> None:
        """Read one full turn from the session, put outputs on _recv_queue."""
        if self._session is None:
            return
        self._first_audio_received = False

        async for message in self._session.receive():
            if message.usage_metadata:
                # Per-turn token bill. prompt_token_count is the input CONTEXT
                # billed this turn — it grows as a long-lived session accumulates
                # history and should drop sharply right after an idle session
                # recycle (see orchestrator._mark_turn_start). Grep
                # "[realtime] Gemini usage" to confirm the reset is cutting cost.
                # Checked FIRST: Gemini ships usage_metadata on the SAME message as
                # turn_complete, and the server_content branch returns on
                # turn_complete — so a check placed after it never runs.
                um = message.usage_metadata
                # Per-model rate table (see _GEMINI_RATES above). Resolved by model
                # name so 2.5 native-audio (cheaper text) and 3.1 Live are billed at
                # their own rates; unknown models fall back to a cost ceiling.
                rates = _gemini_rates_for(self._config.model)
                parts, cost, attributed = [], 0.0, {"in": 0, "out": 0}
                for direction, details in (
                    ("in", um.prompt_tokens_details),
                    ("out", um.response_tokens_details),
                ):
                    for d in details or []:
                        mod = getattr(d.modality, "name", str(d.modality))
                        tok = d.token_count or 0
                        attributed[direction] += tok
                        c = tok * rates.get((direction, mod), 0.0) / 1_000_000
                        cost += c
                        parts.append("%s_%s=%d($%.5f)" % (direction, mod.lower(), tok, c))
                # Tokens Google counted but didn't tag with a modality (system /
                # thinking) — unpriced here, so est is a floor.
                unattr_in = (um.prompt_token_count or 0) - attributed["in"]
                unattr_out = (um.response_token_count or 0) - attributed["out"]
                # Implicit context caching (on by default for Gemini 2.5+/3.1) re-bills
                # the cached prefix — our ~8k-token system-instruction floor — at a 90%
                # discount. `cost` above charges every prompt token at full rate, so
                # subtract the saving on cached tokens to get the REAL estimate. cached
                # tokens are text-in (the floor is text). cached=0 every turn means the
                # cache is not hitting (e.g. session churn) — that's the cost red flag.
                cached = getattr(um, "cached_content_token_count", 0) or 0
                cost_cached = max(0.0, cost - cached * rates[("in", "TEXT")] * 0.90 / 1_000_000)
                usage_logger.info(
                    "[realtime] Gemini usage: model=%s %s +unattr(%din/%dout) | "
                    "cached=%dtok total=%dtok est_full>=$%.5f est_cached>=$%.5f",
                    self._config.model,
                    " ".join(parts) or "-", unattr_in, unattr_out, cached,
                    um.total_token_count or 0, cost, cost_cached,
                )

            if message.server_content:
                content = message.server_content

                # Diagnostic: did Google Search grounding fire this turn? Grounding
                # chunks (web content) get injected into the session context and
                # re-billed as input every later turn — the suspected cause of a
                # sudden persistent in_text jump. Logs the queries + chunk count so
                # we can correlate a search with the cost bump.
                gm = getattr(content, "grounding_metadata", None)
                if gm is not None:
                    queries = list(getattr(gm, "web_search_queries", None) or [])
                    chunks = getattr(gm, "grounding_chunks", None) or []
                    logger.info(
                        "[realtime][grounding] Google Search fired: queries=%s chunks=%d",
                        queries[:3], len(chunks),
                    )

                if content.model_turn and content.model_turn.parts:
                    for part in content.model_turn.parts:
                        # Skip reasoning parts: Gemini flags thought parts with
                        # part.thought=True. Emitting them would speak/show the
                        # model's internal reasoning. Belt-and-suspenders with
                        # include_thoughts=False in the live config.
                        if getattr(part, "thought", False):
                            continue
                        if part.inline_data and part.inline_data.data:
                            if not self._first_audio_received:
                                self._first_audio_received = True
                                if self._speech_ended_at is not None:
                                    latency_ms: float = (
                                        time.monotonic() - self._speech_ended_at
                                    ) * 1000
                                    logger.info("[realtime] Response latency: %.0fms", latency_ms)
                                    self._speech_ended_at = None
                            self._recv_queue.put(
                                OutputEvent(
                                    output=AudioOutput(
                                        audio=pcm16_bytes_to_float32(
                                            part.inline_data.data
                                        )
                                    ),
                                )
                            )
                        elif part.text:
                            # In AUDIO modality with output_audio_transcription
                            # enabled (always, see _build_config), the spoken reply
                            # arrives via output_transcription below. Gemini also
                            # occasionally puts the SAME reply into a model_turn text
                            # part (same quirk as thought parts leaking, filtered
                            # above) — emitting it here too double-speaks the whole
                            # turn (verified on-device 2026-06-29: text part arrived
                            # BEFORE first audio, then output_transcription repeated
                            # it). Skip it; output_transcription is the source of
                            # truth for the spoken text + [HANDLED] transcript.
                            continue

                if content.output_transcription and content.output_transcription.text:
                    self._recv_queue.put(
                        OutputEvent(
                            output=TextOutput(text=content.output_transcription.text),
                        )
                    )

                if content.interrupted:
                    logger.debug("[realtime] Response interrupted")
                    self._first_audio_received = False
                    self._turn_done.set()

                if content.turn_complete:
                    logger.debug("[realtime] Turn complete")
                    self._first_audio_received = False
                    self._turn_done.set()
                    self._recv_queue.put(TurnDoneEvent())
                    return

            elif message.tool_call and message.tool_call.function_calls:
                for fc in message.tool_call.function_calls:
                    logger.debug("[realtime] Function call: %s (call_id=%s)", fc.name, fc.id)
                    self._recv_queue.put(
                        OutputEvent(
                            output=FunctionCallOutput(
                                name=fc.name or "",
                                arguments=json.dumps(fc.args) if fc.args else "{}",
                                call_id=fc.id or "",
                            ),
                        )
                    )

            if message.session_resumption_update:
                update = message.session_resumption_update
                if update.new_handle:
                    self._resumption_handle = update.new_handle

            if message.go_away:
                logger.warning(
                    "Server go_away (time_left=%s)", message.go_away.time_left
                )
                raise ConnectionClosed(None, None)

    # --- Reconnect ---

    def _ensure_connected(self) -> None:
        """Reconnect if not connected. Throttled by an exponential backoff that grows
        on consecutive failures (reset to base on success)."""
        if self._connected.is_set():
            return
        now: float = time.monotonic()
        if now - self._last_reconnect_at < self._reconnect_backoff:
            return
        self._last_reconnect_at = now
        self._reconnect()

    @override
    def force_reconnect(self) -> None:
        """Recover a zombie session: close the live session so the recv loop —
        which may be blocked in `async for session.receive()` on a dead socket —
        unblocks and rebuilds a fresh session via _ensure_connected. Resets the
        reconnect throttle so reconnect happens immediately, not after
        reconnect_delay_s."""
        logger.warning("[realtime] Forcing reconnect — session looks zombie (silent)")
        self._connected.clear()
        self._activity_started = False
        self._turn_done.set()  # unblock any waiting commit
        self._last_reconnect_at = 0.0  # bypass _ensure_connected throttle
        self._reconnect_backoff = self._reconnect_delay_s  # zombie recovery → retry now
        if self._loop is not None:
            try:
                self._submit_and_wait(self._async_disconnect(), timeout=10.0)
            except Exception as e:
                logger.warning("[realtime] force_reconnect disconnect failed: %s", e)
        self._session = None
        # The recv/send loops' _ensure_connected now rebuilds a fresh session.

    def _reconnect(self) -> None:
        self._connected.clear()
        self._activity_started = False
        self._turn_done.set()  # unblock any waiting commit
        if self._loop is None:
            logger.error("[realtime] Cannot reconnect — event loop is None")
            return
        try:
            logger.info("[realtime] Reconnecting...")
            self._submit_and_wait(self._async_disconnect())
            self._submit_and_wait(self._async_connect())
            self._connected.set()
            self._reconnect_backoff = self._reconnect_delay_s  # success → reset backoff
        except Exception as e:
            # Grow backoff so a persistent failure (e.g. usage-limit 4029) doesn't
            # hammer the endpoint every base delay.
            self._reconnect_backoff = min(
                self._reconnect_backoff * 2, self._reconnect_backoff_max
            )
            logger.warning(
                "[realtime] Reconnect failed: %s — next retry in ~%.0fs",
                e, self._reconnect_backoff,
            )

    def _fail_fast_turn(self, reason: str) -> None:
        """End the current turn immediately on a non-idle recv error.

        Without this, any backend failure (proxy go_away, quota / resource
        exhausted, unexpected WS close — anything that is NOT a benign idle
        close 1000) leaves the consumer blocked in receive() for the full
        REALTIME_RECV_QUEUE_TIMEOUT_S before the turn falls back to the main
        agent. Pushing a TurnDoneEvent unblocks receive() now so the main agent
        answers without the dead-air wait. Only fires while a turn is actually
        awaiting output (_turn_done clear); a stray sentinel between turns would
        just be dropped by flush_output() at the next turn start anyway. Late
        output from a successful reconnect is likewise flushed next turn.
        """
        if self._turn_done.is_set():
            return  # no turn awaiting output — nothing to unblock
        self._first_audio_received = False
        self._turn_done.set()
        self._recv_queue.put(TurnDoneEvent())
        logger.info(
            "[realtime] Recv error (%s) — ending turn now, falling back to main "
            "(skipping receive timeout wait)",
            reason,
        )

    # --- VoiceAgentBase implementation ---

    def _submit_and_wait(self, coro: Any, timeout: float = 30.0) -> Any:
        """Submit a coroutine to the IO thread's loop and block until done."""
        if self._loop is None:
            raise RuntimeError("Event loop is None")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(
            timeout=timeout
        )

    @override
    def _do_connect(self) -> None:
        """Spawn IO thread with event loop, connect on it. Blocks until ready."""
        self._loop = asyncio.new_event_loop()
        self._io_thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="gemini-io",
        )
        self._io_thread.start()
        self._submit_and_wait(self._async_connect())

    @override
    def _do_disconnect(self) -> None:
        if self._loop is not None:
            try:
                self._submit_and_wait(self._async_disconnect())
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._io_thread is not None:
                self._io_thread.join(timeout=self._join_timeout_s)
                self._io_thread = None
            self._loop.close()
            self._loop = None

    @override
    def _send_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._loop is None:
                break
            try:
                event: AgentInputEvent = self._send_queue.get(
                    timeout=self._queue_poll_s
                )
            except queue.Empty:
                continue

            for attempt in range(self._max_retries):
                self._ensure_connected()
                if not self._connected.is_set():
                    logger.debug("[realtime] Not connected, skipping attempt %d/%d", attempt + 1, self._max_retries)
                    continue
                try:
                    if isinstance(event, AudioCommitEvent):
                        if not self._turn_done.wait(timeout=10.0):
                            logger.warning("[realtime] Timed out waiting for turn to finish — forcing commit")
                        self._submit_and_wait(
                            self._async_commit(), timeout=self._send_timeout_s
                        )
                    elif isinstance(event, InputEvent) and event.input is not None:
                        self._submit_and_wait(
                            self._async_send_input(event.input),
                            timeout=self._send_timeout_s,
                        )
                    break  # Success
                except (ConnectionClosed, genai_errors.APIError) as e:
                    logger.exception("[realtime] Send failed (attempt %d/%d): %s", attempt + 1, self._max_retries, e)
                    self._reconnect()
                except Exception as e:
                    logger.exception("[realtime] Send error (attempt %d/%d): %s", attempt + 1, self._max_retries, e)
                    self._reconnect()

    @override
    def _recv_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._loop is None:
                break
            if not self._connected.is_set():
                # Proactively reconnect (throttled by reconnect_delay_s) so the
                # session SELF-HEALS while disconnected even with NO audio flowing.
                # Critical after a usage-limit (4029) close: voice_service stops
                # feeding audio (orchestrator.available=False once disconnected), so
                # the send loop never triggers a reconnect — without this the session
                # would stay dead forever even after the limit is lifted.
                self._ensure_connected()
                if not self._connected.is_set():
                    _ = self._connected.wait(timeout=self._queue_poll_s)
                continue

            for attempt in range(self._max_retries):
                self._ensure_connected()
                if not self._connected.is_set():
                    logger.debug("[realtime] Not connected, skipping attempt %d/%d", attempt + 1, self._max_retries)
                    continue
                try:
                    self._submit_and_wait(
                        self._async_receive_turn(), timeout=self._recv_timeout_s
                    )
                    break  # Success — turn received
                except ConnectionClosed as e:
                    code: int | None = getattr(getattr(e, "rcvd", None), "code", None)
                    if code == 1000:
                        logger.info("[realtime] Session closed normally (idle) — reconnecting")
                    else:
                        logger.warning("[realtime] Recv failed (attempt %d/%d): %s", attempt + 1, self._max_retries, e)
                    # Fail-fast on ANY close, including a benign idle-1000: if it
                    # lands mid-turn the fresh session has no committed audio and
                    # will never answer, so end the turn now instead of waiting out
                    # the receive timeout. No-op when genuinely idle between turns
                    # (_turn_done already set).
                    self._fail_fast_turn(f"ws close {code}")
                    self._connected.clear()
                    self._session = None
                except genai_errors.APIError as e:
                    # The genai SDK surfaces a normal WS close (code 1000 — idle /
                    # session-duration timeout) as an APIError whose str is
                    # "1000 None", not as ConnectionClosed. Mirror the 1000 special
                    # case in the ConnectionClosed branch above: log at INFO so a
                    # benign idle-reconnect doesn't show up as a red WARNING.
                    code_str: str = str(e).split(" ", 1)[0]
                    if code_str == "1000":
                        logger.info("[realtime] Session closed normally (idle) — reconnecting")
                    else:
                        logger.warning("[realtime] Recv API error (attempt %d/%d): %s", attempt + 1, self._max_retries, e)
                    # Fail-fast on ANY close (see ConnectionClosed branch): a mid-turn
                    # idle-1000 loses the turn on the fresh session. No-op when idle.
                    self._fail_fast_turn(f"api {code_str}")
                    self._connected.clear()
                    self._session = None
                except Exception as e:
                    logger.exception("[realtime] Unexpected recv error (attempt %d/%d): %s", attempt + 1, self._max_retries, e)
                    self._fail_fast_turn("unexpected")
                    self._connected.clear()
                    self._session = None
