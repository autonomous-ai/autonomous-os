"""Qwen Omni Realtime voice agent — raw WebSocket, queue-based threading, fully sync.

DashScope's realtime API (Model Studio, intl/Singapore MaaS host) speaks the
OpenAI Realtime *beta* event vocabulary (session.update, input_audio_buffer.*,
response.audio.delta, response.done) over its own WS path
`/api-ws/v1/realtime?model=...` with `Authorization: Bearer <key>`. The OpenAI
python SDK client can't be pointed at it (the SDK emits/parses the GA schema —
response.OUTPUT_audio.delta etc.), so this agent drives the socket directly with
`websockets.sync.client`, reusing the same thread/queue skeleton as
openai_realtime.py and the manual-commit turn flow (HAL does its own VAD:
append → commit → response.create).

Audio: input 16 kHz mono pcm16 (base64), output 24 kHz mono pcm16 (base64).
"""

import base64
import json
import logging
import queue
import threading
import time
from typing import Any, override

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection
from websockets.sync.client import connect as ws_connect

from hal.drivers.realtime.config import QwenConfig
from hal.drivers.realtime.exceptions import QwenRealtimeError
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
from hal.drivers.realtime.utils import (
    base64_pcm16_to_float32,
    float32_to_base64_pcm16,
)
from hal.drivers.realtime.voice_agent.base import VoiceAgentBase

logger = logging.getLogger(__name__)
# Per-turn token/cost lines go to their own file (qwen_usage.log) via a
# dedicated logger configured in server_support/log_setup.py (propagate=False)
# — the qwen twin of gemini_usage.log, so the two providers can be compared
# line-for-line.
usage_logger = logging.getLogger("hal.realtime.usage.qwen")

# Qwen realtime pricing, USD per 1M tokens, keyed (direction, modality), PER
# MODEL — same shape as gemini_live._GEMINI_RATES so the cost lines compare 1:1.
#
# BILL-VERIFIED 2026-07-06 (consumedetailbill CSV, intl/Singapore list prices).
# Alibaba publishes none of this on a public page — the widely-mirrored
# "$0.27 in / $1.07 out" turns out to be only turbo's two CHEAPEST line items
# (text_input_token / purein_text_output_token); audio carries a large premium.
# Output on audio-modality turns bills as ONE line item covering text+audio
# tokens (turbo `multi_output_token` $8.89, 3.5 `omni_audio_output_token`
# $62.00), so both out cells carry that rate; turbo's $1.07
# purein_text_output only applies to text-only responses (not our turn shape).
# Search bills separately: $0.01 per search request (search_count), not
# modelled here. Audio→token conversion: turbo 25 tok/s both ways; 3.5 ≈7
# tok/s in, ≈12.5 tok/s out (probe-matched).
_QWEN_RATES: dict[str, dict[tuple[str, str], float]] = {
    "qwen-omni-turbo-realtime": {
        ("in", "TEXT"): 0.27, ("in", "AUDIO"): 4.44,
        ("out", "TEXT"): 8.89, ("out", "AUDIO"): 8.89,
    },
    # Flash bills under the SAME cheap line items as turbo (text_input_token /
    # audio_input_token / multi_output_token — bill-verified 2026-07-06, incl.
    # search-enabled sessions), NOT the omni_* premium items of plus. That
    # makes flash's dominant cost (text in) ~2.8x cheaper than Gemini 3.1.
    "qwen3.5-omni-flash-realtime": {
        ("in", "TEXT"): 0.27, ("in", "AUDIO"): 4.44,
        ("out", "TEXT"): 8.89, ("out", "AUDIO"): 8.89,
    },
    "qwen3.5-omni-plus-realtime": {
        ("in", "TEXT"): 2.10, ("in", "AUDIO"): 16.50,
        ("out", "TEXT"): 62.00, ("out", "AUDIO"): 62.00,
    },
}
# Unknown model → most expensive table (cost ceiling, mirrors gemini_live).
_QWEN_RATES_FALLBACK: dict[tuple[str, str], float] = max(
    _QWEN_RATES.values(), key=lambda r: r[("out", "TEXT")]
)


def _qwen_rates_for(model: str) -> dict[tuple[str, str], float]:
    """Resolve the per-1M-token rate table for a model name (substring match);
    unknown models fall back to the most expensive table (cost = ceiling)."""
    for key, table in _QWEN_RATES.items():
        if key in model:
            return table
    return _QWEN_RATES_FALLBACK


# DashScope constraint (live-probed 2026-07-06): `enable_search` switches the
# session into "agent mode", which REJECTS function tools ("Agent mode does not
# support tools", 400 on first response). So with search on, delegation runs
# over a TEXT MARKER protocol instead: the model replies exactly
# "[DELEGATE] <message>", the recv loop swallows that transcript and synthesizes
# the same FunctionCallOutput a real tool call would have produced — the
# orchestrator can't tell the difference. The model's own audio (which speaks
# the marker) is never played: native audio is off, HAL speaks the transcript.
_DELEGATE_MARKER = "[delegate]"
_MARKER_PROTOCOL_SUFFIX = (
    "\n\n[TOOL PROTOCOL] This session has NO function tools (web search replaces"
    " them). Wherever your instructions say to call delegate_to_main(message),"
    " instead reply with EXACTLY this text and NOTHING else: [DELEGATE] <message>."
    " No words before or after the marker line. Never say \"okay, I'll do it\" —"
    " only the [DELEGATE] line gets it done. All other delegation rules"
    " (what to delegate vs answer directly) still apply. There is no"
    " express_emotion tool — never mention or fake tool calls."
    "\nExamples:"
    "\nUser: \"Turn the volume up a bit\" -> [DELEGATE] Increase volume"
    "\nUser: \"Remind me to take my medicine at 7 PM\" -> [DELEGATE] Set a reminder at 7 PM: take medicine"
)


def _realtime_ws_url(base_url: str, model: str) -> str:
    """Build the realtime WS URL from an operator base_url and model.

    Accepts http(s) or ws(s) scheme, with or without a trailing `/realtime`,
    e.g. `https://<host>/api-ws/v1` → `wss://<host>/api-ws/v1/realtime?model=m`.
    """
    url = base_url.strip().rstrip("/")
    if url.startswith("https://"):
        url = "wss://" + url[len("https://"):]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://"):]
    if not url.endswith("/realtime"):
        url += "/realtime"
    return f"{url}?model={model}"


class QwenRealtimeAgent(VoiceAgentBase):
    def __init__(
        self,
        config: QwenConfig,
        tools: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(tools=tools)
        self._config: QwenConfig = config
        self._connection: ClientConnection | None = None
        # Serializes access to self._connection across send/recv threads —
        # same discipline as openai_realtime (recv iteration runs OUTSIDE the
        # lock on a snapshot so sends aren't starved mid-turn).
        self._conn_lock: threading.RLock = threading.RLock()
        self._speech_ended_at: float | None = None
        self._reconnect_delay_s: float = config.reconnect_delay_s
        self._max_retries: int = config.max_retries
        self._last_reconnect_at: float = 0.0
        self._reconnect_backoff: float = config.reconnect_delay_s
        self._reconnect_backoff_max: float = 60.0
        # Set when the model is idle (no active response); cleared on
        # response.create, set again on response.done.
        self._turn_done: threading.Event = threading.Event()
        self._turn_done.set()
        # True when delegation runs over the [DELEGATE] text-marker protocol
        # (search on → no function tools). Finalized in _sync_connect.
        self._marker_delegate: bool = False

    @property
    @override
    def sample_rate(self) -> int:
        return self._config.sample_rate  # input/mic rate: 16 kHz

    @property
    @override
    def output_sample_rate(self) -> int:
        # Qwen omni realtime always emits 24 kHz PCM regardless of input rate.
        return 24000

    # --- Sync internals ---

    def _sync_connect(self) -> None:
        url = _realtime_ws_url(self._config.base_url, self._config.model)
        logger.info(
            "Connecting to Qwen Omni Realtime API (url=%s, model=%s)",
            url.split("?")[0], self._config.model,
        )
        self._connection = ws_connect(
            url,
            additional_headers={"Authorization": f"Bearer {self._config.api_key}"},
            max_size=16 * 1024 * 1024,  # audio deltas are large base64 frames
        )

        session: dict[str, Any] = {
            # text+audio: the reply text arrives as response.audio_transcript
            # deltas; audio-less "text" mode changes the event set entirely.
            "modalities": ["text", "audio"],
            "voice": self._config.voice.value,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "instructions": self._config.instructions,
            # HAL runs its own local VAD (append → commit → response.create),
            # so server turn detection stays off — mirrors the other providers.
            "turn_detection": None,
        }
        if self._config.search_enabled:
            # Built-in web search (3.5 models) — the qwen twin of Gemini's
            # Google Search grounding. Without it the model answers live-data
            # questions from stale knowledge (probed 2026-07-06). Search and
            # function tools are mutually exclusive (see _DELEGATE_MARKER), so
            # delegation switches to the marker protocol.
            session["enable_search"] = True
            if self._tools:
                session["instructions"] = (
                    self._config.instructions + _MARKER_PROTOCOL_SUFFIX
                )
        elif self._tools:
            # Beta-style flat tool entries; our registry (DELEGATE_TOOL etc.)
            # is already in this exact shape. If the model rejects tools the
            # server answers with an `error` event — visible in the logs, and
            # the session itself stays usable.
            session["tools"] = self._tools
            session["tool_choice"] = "auto"
        self._marker_delegate: bool = bool(self._config.search_enabled and self._tools)

        self._send_event({"type": "session.update", "session": session})
        logger.info(
            "[realtime] Qwen Realtime session open (voice=%s)", self._config.voice
        )

    def _sync_disconnect(self) -> None:
        if self._connection is not None:
            logger.info("[realtime] Disconnecting from Qwen Omni Realtime API")
            try:
                self._connection.close()
            except Exception:  # already dead — closing is best-effort
                pass
            self._connection = None

    def _send_event(self, event: dict[str, Any]) -> None:
        """Serialize and send one client event. Caller holds no guarantees —
        grabs the connection lock itself."""
        with self._conn_lock:
            if self._connection is None:
                return
            self._connection.send(json.dumps(event))

    def _sync_send_input(self, input: InputBase) -> None:
        if isinstance(input, AudioInput):
            self._send_event({
                "type": "input_audio_buffer.append",
                "audio": float32_to_base64_pcm16(input.audio),
            })

        elif isinstance(input, TextInput):
            self._send_event({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": input.text}],
                },
            })

        elif isinstance(input, ImageInput):
            # qwen-omni-turbo-realtime is text+audio only (no image input) —
            # drop with a log instead of erroring the send loop. The `look`
            # tool is Gemini-gated in the orchestrator, so this only triggers
            # if an image is force-fed to the agent.
            logger.warning(
                "[realtime] Qwen realtime model %s does not accept images — dropped",
                self._config.model,
            )

        elif isinstance(input, FunctionCallResultInput):
            self._send_event({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": input.call_id,
                    "output": input.output,
                },
            })
            # Fire-and-forget tools (trigger_response=False) only record the
            # result — no fresh response, no second reply.
            if input.trigger_response:
                self._safe_response_create()

    def _sync_commit(self) -> None:
        self._send_event({"type": "input_audio_buffer.commit"})
        self._safe_response_create()

    def _safe_response_create(self) -> None:
        """Wait for any active response to finish, then create a new one."""
        if not self._turn_done.wait(timeout=10.0):
            logger.warning(
                "[realtime] Timed out waiting for active response to finish — forcing new response"
            )
        with self._conn_lock:
            if self._connection is None:
                return
            self._turn_done.clear()
            self._speech_ended_at = time.monotonic()
            # Explicit response modalities are REQUIRED for audio out: with a
            # bare response.create the server answered text-only even though
            # the session was configured ["text","audio"] (probed 2026-07-06).
            self._connection.send(json.dumps({
                "type": "response.create",
                "response": {"modalities": ["text", "audio"]},
            }))

    def _log_usage(self, response: dict[str, Any]) -> None:
        """Per-turn token/cost line → qwen_usage.log (gemini_live twin).

        input_tokens is the input CONTEXT billed this turn — grows with session
        history, drops after a session recycle (orchestrator idle/turn-cap).
        """
        usage = response.get("usage") or {}
        if not usage:
            return
        in_det = usage.get("input_tokens_details") or {}
        out_det = usage.get("output_tokens_details") or {}
        rates = _qwen_rates_for(self._config.model)
        cost = (
            in_det.get("text_tokens", 0) * rates[("in", "TEXT")]
            + in_det.get("audio_tokens", 0) * rates[("in", "AUDIO")]
            + out_det.get("text_tokens", 0) * rates[("out", "TEXT")]
            + out_det.get("audio_tokens", 0) * rates[("out", "AUDIO")]
        ) / 1_000_000
        # Tokens the details didn't attribute to a modality get charged at the
        # pricier direction rate so the line is a ceiling, not an under-report.
        unattr_in = max(0, (usage.get("input_tokens") or 0) - sum(in_det.values()))
        unattr_out = max(0, (usage.get("output_tokens") or 0) - sum(out_det.values()))
        cost += (
            unattr_in * max(rates[("in", "TEXT")], rates[("in", "AUDIO")])
            + unattr_out * max(rates[("out", "TEXT")], rates[("out", "AUDIO")])
        ) / 1_000_000
        usage_logger.info(
            "[realtime] Qwen usage: model=%s in(text=%d,audio=%d) "
            "out(text=%d,audio=%d) +unattr(%din/%dout) total=%s cost=$%.6f",
            self._config.model,
            in_det.get("text_tokens", 0), in_det.get("audio_tokens", 0),
            out_det.get("text_tokens", 0), out_det.get("audio_tokens", 0),
            unattr_in, unattr_out,
            usage.get("total_tokens", 0), cost,
        )

    def _sync_receive_turn(self, conn: ClientConnection) -> bool:
        """Read one full turn from `conn`, put outputs on _recv_queue.

        Returns True when the turn ended normally (`response.done` seen), False
        when the socket closed cleanly without one — the caller fail-fasts the
        turn in that case (same contract as openai_realtime).
        """
        # Marker-protocol state for THIS turn: transcript deltas are held back
        # until the head of the reply proves it is / is not a "[DELEGATE] ..."
        # line (a few chars, sub-100ms). mode: None=undecided, "text"=pass
        # through, "delegate"=swallow and synthesize a tool call at turn end.
        tx_mode: str | None = None
        tx_buf: str = ""
        while True:
            try:
                raw = conn.recv()
            except ConnectionClosed as e:
                # Normal close (1000) between turns is a clean end of
                # iteration; anything else propagates as an error.
                if e.rcvd is not None and e.rcvd.code == 1000:
                    return False
                raise
            try:
                event: dict[str, Any] = json.loads(raw)
            except (TypeError, ValueError):
                logger.warning("[realtime] Non-JSON frame from Qwen WS — skipped")
                continue

            match event.get("type"):
                case "input_audio_buffer.speech_stopped":
                    self._speech_ended_at = time.monotonic()

                case "response.audio.delta":
                    if self._speech_ended_at is not None:
                        latency_ms: float = (
                            time.monotonic() - self._speech_ended_at
                        ) * 1000
                        logger.info("[realtime] Response latency: %.0fms", latency_ms)
                        self._speech_ended_at = None
                    self._recv_queue.put(
                        OutputEvent(
                            output=AudioOutput(
                                audio=base64_pcm16_to_float32(event.get("delta", ""))
                            ),
                        )
                    )

                case "response.audio_transcript.delta":
                    # The ONLY text source we emit. response.text.delta is
                    # deliberately ignored below: with modalities
                    # ["text","audio"] both channels can carry the reply and
                    # emitting both would double it (the gemini part.text /
                    # output_transcription double-reply bug, relearned).
                    delta: str = event.get("delta", "")
                    if not self._marker_delegate or tx_mode == "text":
                        self._recv_queue.put(OutputEvent(output=TextOutput(text=delta)))
                        continue
                    tx_buf += delta
                    if tx_mode == "delegate":
                        continue
                    head = tx_buf.lstrip().lower()
                    if len(head) < len(_DELEGATE_MARKER):
                        if _DELEGATE_MARKER.startswith(head):
                            continue  # still a possible marker prefix — hold
                    elif head.startswith(_DELEGATE_MARKER):
                        tx_mode = "delegate"
                        continue
                    # Proven normal text — flush what was held and stream on.
                    tx_mode = "text"
                    self._recv_queue.put(OutputEvent(output=TextOutput(text=tx_buf)))

                case "response.function_call_arguments.done":
                    logger.debug(
                        "Function call: %s (call_id=%s)",
                        event.get("name"), event.get("call_id"),
                    )
                    self._recv_queue.put(
                        OutputEvent(
                            output=FunctionCallOutput(
                                name=event.get("name", ""),
                                arguments=event.get("arguments", "{}"),
                                call_id=event.get("call_id", ""),
                            ),
                        )
                    )

                case "response.done":
                    logger.debug("[realtime] Response complete")
                    if tx_mode == "delegate":
                        # Synthesize the tool call the marker stands in for —
                        # downstream (orchestrator DelegateSignal) is identical
                        # to a real function call.
                        message = tx_buf.lstrip()[len(_DELEGATE_MARKER):].strip()
                        logger.info(
                            "[realtime] Marker delegate → delegate_to_main(%r)",
                            message,
                        )
                        self._recv_queue.put(
                            OutputEvent(
                                output=FunctionCallOutput(
                                    name="delegate_to_main",
                                    arguments=json.dumps({"message": message}),
                                    call_id="qwen-marker-delegate",
                                ),
                            )
                        )
                    elif tx_mode is None and tx_buf.strip():
                        # Reply shorter than the marker ("OK.") — never resolved;
                        # flush it so short answers aren't swallowed.
                        self._recv_queue.put(
                            OutputEvent(output=TextOutput(text=tx_buf))
                        )
                    self._log_usage(event.get("response") or {})
                    self._turn_done.set()
                    self._recv_queue.put(TurnDoneEvent())
                    return True

                case "error":
                    err = event.get("error") or {}
                    logger.error("[realtime] Qwen Realtime API error: %s", err)
                    raise QwenRealtimeError(f"Realtime API error: {err}")

                case _:
                    pass

    # --- Reconnect (identical discipline to openai_realtime) ---

    def _ensure_connected(self) -> None:
        if self._connected.is_set():
            return
        now: float = time.monotonic()
        if now - self._last_reconnect_at < self._reconnect_backoff:
            return
        self._last_reconnect_at = now
        self._reconnect()

    def _reconnect(self) -> None:
        with self._conn_lock:
            if self._connected.is_set():
                return
            self._turn_done.set()  # unblock any waiting commit
            try:
                logger.info("[realtime] Reconnecting...")
                self._sync_disconnect()
                self._sync_connect()
                self._connected.set()
                self._reconnect_backoff = self._reconnect_delay_s
            except Exception as e:
                self._reconnect_backoff = min(
                    self._reconnect_backoff * 2, self._reconnect_backoff_max
                )
                logger.warning(
                    "[realtime] Reconnect failed: %s — next retry in ~%.0fs",
                    e, self._reconnect_backoff,
                )

    def _fail_fast_turn(self, reason: str) -> None:
        """End the current turn immediately on a recv error (see openai_realtime)."""
        if self._turn_done.is_set():
            return
        self._turn_done.set()
        self._recv_queue.put(TurnDoneEvent())
        logger.info(
            "[realtime] Recv error (%s) — ending turn now, falling back to main "
            "(skipping receive timeout wait)",
            reason,
        )

    def _drop_connection(self, conn: ClientConnection | None) -> None:
        with self._conn_lock:
            if conn is None or self._connection is conn:
                self._connected.clear()
                self._connection = None

    # --- VoiceAgentBase implementation ---

    @override
    def _do_connect(self) -> None:
        self._sync_connect()

    @override
    def _do_disconnect(self) -> None:
        self._sync_disconnect()

    @override
    def _send_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                event: AgentInputEvent = self._send_queue.get(timeout=1)
            except queue.Empty:
                continue

            for attempt in range(self._max_retries):
                self._ensure_connected()
                if not self._connected.is_set():
                    logger.debug(
                        "[realtime] Not connected, skipping attempt %d/%d",
                        attempt + 1, self._max_retries,
                    )
                    continue
                conn: ClientConnection | None = self._connection
                try:
                    if isinstance(event, AudioCommitEvent):
                        self._sync_commit()
                    elif isinstance(event, InputEvent) and event.input is not None:
                        self._sync_send_input(event.input)
                    break  # Success
                except Exception as e:
                    logger.exception(
                        "[realtime] Send failed (attempt %d/%d): %s",
                        attempt + 1, self._max_retries, e,
                    )
                    self._drop_connection(conn)

    @override
    def _recv_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._connected.is_set():
                self._ensure_connected()
                if not self._connected.is_set():
                    self._connected.wait(timeout=1)
                continue

            for attempt in range(self._max_retries):
                self._ensure_connected()
                with self._conn_lock:
                    conn: ClientConnection | None = (
                        self._connection if self._connected.is_set() else None
                    )
                if conn is None:
                    logger.debug(
                        "[realtime] Not connected, skipping attempt %d/%d",
                        attempt + 1, self._max_retries,
                    )
                    continue
                try:
                    completed: bool = self._sync_receive_turn(conn)
                    if not completed:
                        self._fail_fast_turn("connection closed mid-turn")
                        self._drop_connection(conn)
                    break  # Success
                except QwenRealtimeError as e:
                    logger.warning(
                        "[realtime] Recv failed (attempt %d/%d): %s",
                        attempt + 1, self._max_retries, e,
                    )
                    self._fail_fast_turn("api error")
                    self._drop_connection(conn)
                except Exception as e:
                    logger.exception(
                        "[realtime] Unexpected recv error (attempt %d/%d): %s",
                        attempt + 1, self._max_retries, e,
                    )
                    self._fail_fast_turn("unexpected")
                    self._drop_connection(conn)
