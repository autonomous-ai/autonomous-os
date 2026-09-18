"""OpenAI Realtime voice agent implementation — queue-based threading, fully sync.

Speaks the GA Realtime API (`openai.resources.realtime`, openai >= 3.x): session
shape `type: realtime` + `audio.input/output`, event names
`response.output_audio.delta` / `response.output_audio_transcript.delta`.

Contract parity with gemini_live.py — everything the live pump and the turn
path key on is emitted here too:
  - every output and the TurnDoneEvent carry the provider user-turn key
    (`user_turn_id`), frozen per response so a late input transcription can
    never re-own an earlier reply;
  - server VAD (`speech_started` / `speech_stopped`) and input transcription
    surface as UserSpeechOutput (LIVE_MODE only, like Gemini);
  - a barge-in drains the recv queue, emits InterruptedOutput(server_interrupt),
    bumps the output generation and truncates the assistant item on the server
    so the model's context matches what the user actually heard;
  - OutputEvent.gen is bumped per response and per interruption so
    VoiceAgentBase.receive() drops audio from a cancelled reply;
  - note_server_activity() on every inbound event feeds the silent-turn
    watchdog (a reasoning-heavy or tool turn is not "silent" on the wire);
  - a per-turn token/cost line goes to openai_usage.log.
"""

import base64
import logging
import queue
import threading
import time
from typing import Any, override
from uuid import uuid4

import cv2
import numpy as np
from openai import OpenAI
from openai.resources.realtime.realtime import RealtimeConnection

from hal import config as app_config
from hal.realtime.config import OpenAIConfig
from hal.realtime.enums import OpenAITurnDetectionType
from hal.realtime.exceptions import OpenAIRealtimeError
from hal.realtime.models import (
    AgentInputEvent,
    AudioCommitEvent,
    AudioInput,
    AudioOutput,
    ExecutionOutput,
    FunctionCallOutput,
    FunctionCallResultInput,
    ImageInput,
    InputBase,
    InputEvent,
    InterruptedOutput,
    OutputEvent,
    TextInput,
    TextOutput,
    TurnDoneEvent,
    UserSpeechOutput,
)
from hal.realtime.utils import (
    base64_pcm16_to_float32,
    float32_to_base64_pcm16,
)
from hal.realtime.voice_agent.base import VoiceAgentBase

logger = logging.getLogger(__name__)
# Per-turn token/cost lines go to their own file (openai_usage.log) via a
# dedicated child logger configured in server_support/log_setup.py
# (propagate=False), so they don't mix into server.log or gemini_usage.log.
usage_logger = logging.getLogger("hal.realtime.usage.openai")

# OpenAI Realtime pricing, USD per 1M tokens, keyed (direction, modality), PER
# MODEL. Source: developers.openai.com/api/docs/pricing (verified 2026-09-16).
# "cached" is the discounted rate for the prompt-cache hit reported in
# input_token_details.cached_tokens. Keys are matched IN ORDER as a SUBSTRING of
# self._config.model: "mini" first so "gpt-realtime-2-mini" never resolves to the
# full-size "gpt-realtime-2" row, and "gpt-realtime-2" before "gpt-realtime" so
# the dated GA model does not fall through to the older text-out rate.
_OPENAI_RATES: tuple[tuple[str, dict[tuple[str, str], float]], ...] = (
    ("mini", {
        ("in", "TEXT"): 0.60, ("in", "AUDIO"): 10.0,
        ("out", "TEXT"): 2.40, ("out", "AUDIO"): 20.0,
        ("cached", "TEXT"): 0.06, ("cached", "AUDIO"): 0.30,
    }),
    ("gpt-realtime-2", {
        ("in", "TEXT"): 4.0, ("in", "AUDIO"): 32.0,
        ("out", "TEXT"): 24.0, ("out", "AUDIO"): 64.0,
        ("cached", "TEXT"): 0.40, ("cached", "AUDIO"): 0.40,
    }),
    ("gpt-realtime", {
        ("in", "TEXT"): 4.0, ("in", "AUDIO"): 32.0,
        ("out", "TEXT"): 16.0, ("out", "AUDIO"): 64.0,
        ("cached", "TEXT"): 0.40, ("cached", "AUDIO"): 0.40,
    }),
)
# Unknown model → the table with the highest text-out rate, so an untabled
# model logs a cost CEILING rather than an under-report (same rule as Gemini).
_OPENAI_RATES_FALLBACK: dict[tuple[str, str], float] = max(
    (table for _, table in _OPENAI_RATES), key=lambda r: r[("out", "TEXT")]
)

# LIVE_VAD_START_SENSITIVITY → server_vad `threshold` (API default 0.5). "low"
# sensitivity = an onset needs LOUDER evidence = higher threshold — the OpenAI
# counterpart of Gemini's START_SENSITIVITY_LOW echo defence (see
# GeminiLiveAgent._activity_detection). HAL_OPENAI_VAD_THRESHOLD overrides.
_VAD_THRESHOLDS: dict[str, float] = {"low": 0.7, "high": 0.3}

# Client event_id stamped on our best-effort `conversation.item.truncate`. The
# server answers a truncate past the real audio length with an `error` event;
# that must never tear the session down, so errors carrying this id are benign.
_TRUNCATE_EVENT_ID: str = "hal-truncate"
# Realtime API error codes that describe a harmless client race, not a broken
# session: committing an empty buffer (local VAD ended a turn shorter than the
# 100 ms minimum), creating a response while one is active (server VAD and a
# client commit crossed), cancelling a response that already finished.
_BENIGN_ERROR_CODES: frozenset[str] = frozenset({
    "input_audio_buffer_commit_empty",
    "conversation_already_has_active_response",
    "response_cancel_not_active",
})
# Bound on the item_id → user-turn map so a long live session cannot grow it.
_MAX_USER_ITEMS: int = 16


def _openai_rates_for(model: str) -> dict[tuple[str, str], float]:
    """Resolve the per-1M-token rate table for a model name (ordered substring
    match); unknown models fall back to the most expensive table (cost = ceiling)."""
    for key, table in _OPENAI_RATES:
        if key in model:
            return table
    return _OPENAI_RATES_FALLBACK


class OpenAIRealtimeAgent(VoiceAgentBase):
    """OpenAI Realtime provider.

    Base-class hooks deliberately left at their defaults, with the reason:
      - `end_turn()` stays a no-op: OpenAI sends `response.done` even for a
        function-call-only response, so `_turn_done` is always released by the
        recv loop and the next `response.create` never deadlocks. Forcing it
        would race a still-active response into
        `conversation_already_has_active_response`.
      - `requires_fresh_session` stays False: a `function_call_output` item can
        be recorded without a response, so an unacknowledged tool call never
        poisons the session the way it does on Gemini (1008).
      - `output_sample_rate` stays the input rate: PCM in and out are both 24 kHz.
    """

    def __init__(
        self,
        config: OpenAIConfig,
        tools: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(tools=tools)
        self._config: OpenAIConfig = config
        self._client: OpenAI = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
        )
        self._connection: RealtimeConnection | None = None
        # Serializes all access to self._connection across the send/recv threads.
        # Reentrant: a send op holds it while triggering _safe_response_create,
        # which re-acquires it on the same thread. The blocking recv iteration
        # runs OUTSIDE the lock (on a snapshot) so sends aren't starved during a
        # turn — only connection swaps, writes, and teardown are serialized. The
        # recv thread takes it briefly for its own writes (item truncate).
        self._conn_lock: threading.RLock = threading.RLock()
        # Per-turn timing markers (monotonic so NTP cannot skew them).
        self._speech_ended_at: float | None = None
        self._commit_sent_at: float | None = None
        self._first_audio_received: bool = False
        self._reconnect_delay_s: float = config.reconnect_delay_s
        self._max_retries: int = config.max_retries
        self._queue_poll_s: float = config.queue_poll_s
        self._response_wait_s: float = config.response_wait_s
        self._last_reconnect_at: float = 0.0
        # Exponential backoff (see gemini_live): grow the reconnect throttle on
        # consecutive failures so the self-reconnecting recv loop doesn't hammer a
        # persistently-failing endpoint; reset to base on success.
        self._reconnect_backoff: float = config.reconnect_delay_s
        self._reconnect_backoff_max: float = 60.0
        # Signals that the model is idle (no active response). Set by default,
        # cleared when response.create() is called, set again on response.done.
        self._turn_done: threading.Event = threading.Event()
        self._turn_done.set()
        # Output generation: bumped per response and per interruption so
        # VoiceAgentBase.receive() can drop audio from a superseded reply.
        self._turn_gen: int = 0
        # Live-mode input ownership (same shape as gemini_live): the key of the
        # user turn currently being captured and whether its UserSpeechOutput
        # has been emitted yet.
        self._live_user_turn_id: str = ""
        self._live_speech_emitted: bool = False
        # Provider transcript of the utterance in flight, handed to
        # FunctionCallOutput.user_transcript (the delegate message source).
        self._user_transcript: str = ""
        # OpenAI ids every user audio item; remembering which live turn each
        # item belongs to lets a transcription that lands after the NEXT
        # speech_started still be attributed to the input it transcribes.
        # Value: (turn_id, transcript text already emitted for that item).
        self._user_items: dict[str, tuple[str, str]] = {}

    @property
    @override
    def sample_rate(self) -> int:
        return self._config.sample_rate

    # --- Session config ---

    def _turn_detection(self) -> dict[str, Any] | None:
        """Provider-side VAD settings, or None for manual (client-bracketed) turns.

        Only sends the knobs that are set, so the API default stands for the
        rest. server_vad: LIVE_VAD_START_SENSITIVITY → threshold (or the
        explicit HAL_OPENAI_VAD_THRESHOLD), LIVE_VAD_PREFIX_PADDING_MS →
        prefix_padding_ms, LIVE_VAD_SILENCE_MS → silence_duration_ms.
        semantic_vad has no threshold; LIVE_VAD_END_SENSITIVITY → eagerness
        (how quickly the model decides the user is done).
        """
        td_type: OpenAITurnDetectionType | None = self._config.turn_detection_type
        if td_type is None:
            return None
        cfg: dict[str, Any] = {"type": td_type.value}
        if td_type == OpenAITurnDetectionType.SERVER_VAD:
            threshold: float = self._config.vad_threshold or _VAD_THRESHOLDS.get(
                self._config.vad_start_sensitivity, 0.0
            )
            if threshold > 0:
                cfg["threshold"] = threshold
            if self._config.vad_prefix_padding_ms > 0:
                cfg["prefix_padding_ms"] = self._config.vad_prefix_padding_ms
            if self._config.vad_silence_ms > 0:
                cfg["silence_duration_ms"] = self._config.vad_silence_ms
        elif self._config.vad_end_sensitivity in ("low", "high"):
            cfg["eagerness"] = self._config.vad_end_sensitivity
        logger.info(
            "[realtime] server VAD: type=%s threshold=%s prefix_padding=%sms "
            "silence=%sms eagerness=%s",
            cfg["type"],
            cfg.get("threshold", "(provider default)"),
            cfg.get("prefix_padding_ms", "(provider default)"),
            cfg.get("silence_duration_ms", "(provider default)"),
            cfg.get("eagerness", "(provider default)"),
        )
        return cfg

    def _build_session(self) -> dict[str, Any]:
        """The `session.update` payload (GA shape)."""
        audio_input: dict[str, Any] = {
            "format": {"type": "audio/pcm", "rate": self._config.sample_rate},
            # None is meaningful: it switches server VAD OFF for manual turns.
            "turn_detection": self._turn_detection(),
            # Input transcription is the ONLY source of the user's words on
            # this side: UserSpeechOutput.transcript, live history and the
            # delegate message all come from it. Off by default in the API.
            "transcription": self._transcription(),
        }
        if self._config.noise_reduction in ("near_field", "far_field"):
            # Filters the buffer before VAD and the model — fewer false VAD
            # onsets from room noise / echo residue, better perception.
            audio_input["noise_reduction"] = {"type": self._config.noise_reduction}

        session: dict[str, Any] = {
            "type": "realtime",
            "instructions": self._config.instructions,
            # Audio only: the transcript stream is the text source. Adding
            # "text" would make the model emit response.output_text.delta AS
            # WELL, and speaking both double-speaks the reply (the Gemini
            # part.text / output_transcription double-reply bug).
            "output_modalities": ["audio"],
            "audio": {
                "input": audio_input,
                "output": {
                    "format": {"type": "audio/pcm", "rate": self._config.sample_rate},
                    "voice": self._config.voice.value,
                },
            },
        }

        if self._tools:
            session["tools"] = self._tools
            session["tool_choice"] = "auto"

        if self._config.reasoning_effort is not None:
            session["reasoning"] = {"effort": self._config.reasoning_effort.value}

        truncation_cfg: dict[str, Any] = {"type": self._config.truncation_type.value}
        if self._config.truncation_type.value == "retention_ratio":
            truncation_cfg["retention_ratio"] = self._config.truncation_retention_ratio
        session["truncation"] = truncation_cfg
        return session

    def _transcription(self) -> dict[str, Any]:
        cfg: dict[str, Any] = {"model": self._config.transcribe_model}
        lang: str | None = self._config.language
        if lang:
            # The API wants ISO-639-1; the device setting may be a BCP-47 tag.
            cfg["language"] = lang.split("-", 1)[0].lower()
        return cfg

    # --- Sync internals ---

    def _sync_connect(self) -> None:
        # A reopened session must not inherit input ownership from the old one.
        self._live_user_turn_id = ""
        self._live_speech_emitted = False
        self._user_transcript = ""
        self._user_items = {}
        logger.info(
            "Connecting to OpenAI Realtime API (base_url=%s, model=%s)",
            self._config.base_url,
            self._config.model,
        )

        self._connection = self._client.realtime.connect(
            model=self._config.model,
        ).enter()
        self._connection.session.update(session=self._build_session())
        logger.info("[realtime] OpenAI Realtime session open (voice=%s)", self._config.voice)

    def _sync_disconnect(self) -> None:
        if self._connection is not None:
            logger.info("[realtime] Disconnecting from OpenAI Realtime API")
            self._connection.close()
            self._connection = None

    def _sync_send_input(self, input: InputBase) -> None:
        with self._conn_lock:
            if self._connection is None:
                return

            if isinstance(input, AudioInput):
                b64_audio: str = float32_to_base64_pcm16(input.audio)
                self._connection.input_audio_buffer.append(audio=b64_audio)

            elif isinstance(input, TextInput):
                self._connection.conversation.item.create(
                    item={
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": input.text}],
                    }
                )

            elif isinstance(input, ImageInput):
                _: bool
                buf: np.ndarray
                _, buf = cv2.imencode(".png", input.image)
                b64_img: str = base64.b64encode(buf.tobytes()).decode("ascii")
                data_uri: str = f"data:image/png;base64,{b64_img}"
                self._connection.conversation.item.create(
                    item={
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_image", "image_url": data_uri}],
                    }
                )

            elif isinstance(input, FunctionCallResultInput):
                self._connection.conversation.item.create(
                    item={
                        "type": "function_call_output",
                        "call_id": input.call_id,
                        "output": input.output,
                    }
                )
                # Fire-and-forget tools (trigger_response=False) only record the
                # result; they must NOT spawn a fresh response or the model would
                # speak a second time and add a full round-trip of latency.
                if input.trigger_response:
                    self._safe_response_create()

    def _sync_commit(self, turn_end_queued_at: float | None = None) -> None:
        with self._conn_lock:
            if self._connection is None:
                return
            if self._config.turn_detection_type is not None:
                # Server VAD owns turn-taking: it commits the buffer and creates
                # the response on its own speech_stopped. A client commit here
                # would land on an already-committed (empty) buffer and a second
                # response.create would collide with the server's. Same rule as
                # GeminiLiveAgent._async_commit, which only sends activityEnd
                # when automatic activity detection is off.
                logger.debug("[realtime] Commit skipped — server VAD brackets the turn")
                return
            self._connection.input_audio_buffer.commit()
            self._commit_sent_at = time.monotonic()
            if turn_end_queued_at is not None:
                logger.info(
                    "[realtime] Turn timing: local_end->commit_sent=%.0fms",
                    (self._commit_sent_at - turn_end_queued_at) * 1000,
                )
            self._safe_response_create()

    def _safe_response_create(self) -> None:
        """Wait for any active response to finish, then create a new one.

        The wait runs before taking the connection lock so the recv thread can
        keep draining events (and set _turn_done) without contending with us.
        """
        if not self._turn_done.wait(timeout=self._response_wait_s):
            logger.warning("[realtime] Timed out waiting for active response to finish — forcing new response")
        with self._conn_lock:
            if self._connection is None:
                return
            self._turn_done.clear()
            self._speech_ended_at = time.monotonic()
            self._connection.response.create()

    # --- Live-mode input ownership ---

    def _observe_user_speech(
        self,
        *,
        turn_id: str | None = None,
        endpoint_at: float | None = None,
        transcript: str = "",
        transcript_finished: bool = False,
    ) -> None:
        """Publish one input key, enriching it only with an actual VAD endpoint.

        `turn_id` pins the observation to an EARLIER input (a transcription that
        lands after the next speech_started); the live capture state is left
        alone in that case.
        """
        if not app_config.LIVE_MODE:
            return
        if turn_id is not None and turn_id != getattr(self, "_live_user_turn_id", ""):
            if not turn_id:
                return
            self._recv_queue.put(OutputEvent(
                gen=getattr(self, "_turn_gen", 0),
                output=UserSpeechOutput(
                    turn_id=turn_id,
                    transcript=transcript,
                    transcript_finished=transcript_finished,
                    user_turn_id=turn_id,
                    endpoint_at=endpoint_at,
                    method="server_vad" if endpoint_at is not None else "provider_transcript",
                ),
            ))
            return
        if transcript_finished and not transcript and endpoint_at is None:
            # Completion-only metadata cannot invent an input turn.
            if not getattr(self, "_live_user_turn_id", ""):
                return
        if not getattr(self, "_live_user_turn_id", ""):
            self._live_user_turn_id = "openai-" + uuid4().hex
            self._live_speech_emitted = False
        if (getattr(self, "_live_speech_emitted", False) and endpoint_at is None
                and not transcript and not transcript_finished):
            return
        self._live_speech_emitted = True
        self._recv_queue.put(OutputEvent(
            gen=getattr(self, "_turn_gen", 0),
            output=UserSpeechOutput(
                turn_id=self._live_user_turn_id,
                transcript=transcript,
                transcript_finished=transcript_finished,
                user_turn_id=self._live_user_turn_id,
                endpoint_at=endpoint_at,
                method="server_vad" if endpoint_at is not None else "provider_transcript",
            ),
        ))

    def _remember_user_item(self, item_id: str | None) -> None:
        """Bind a user audio item to the live turn being captured right now."""
        if not item_id:
            return
        items: dict[str, tuple[str, str]] = getattr(self, "_user_items", None) or {}
        self._user_items = items
        items[item_id] = (getattr(self, "_live_user_turn_id", ""), "")
        while len(items) > _MAX_USER_ITEMS:
            del items[next(iter(items))]

    def _on_input_transcript(self, item_id: str | None, text: str, *, finished: bool) -> None:
        """Route an input transcription chunk to the input turn it belongs to.

        A `completed` event carries the WHOLE transcript; only the part not
        already streamed as deltas is emitted so a consumer that concatenates
        chunks (live history) never sees the utterance twice.
        """
        items: dict[str, tuple[str, str]] = getattr(self, "_user_items", None) or {}
        known = items.get(item_id or "")
        turn_id, emitted = known if known is not None else (None, "")
        if finished:
            chunk = text[len(emitted):] if emitted and text.startswith(emitted) else (
                "" if emitted and text == emitted else text
            )
        else:
            chunk = text
        if known is not None and item_id:
            items[item_id] = (turn_id or "", emitted + chunk)
        if chunk:
            logger.info("[realtime] <<< user said: %r", chunk)
            self._user_transcript = getattr(self, "_user_transcript", "") + chunk
        if chunk or finished:
            self._observe_user_speech(
                turn_id=turn_id, transcript=chunk, transcript_finished=finished,
            )

    # --- Interruption ---

    def _output_rate(self) -> int:
        cfg = getattr(self, "_config", None)
        return cfg.sample_rate if cfg is not None else 24000

    def _handle_interrupt(
        self,
        conn: RealtimeConnection | None,
        *,
        user_turn_id: str,
        item: tuple[str, int] | None,
        received_ms: float,
    ) -> None:
        """The user barged in (or the response was cancelled): stop playback now.

        Mirrors gemini_live's `interrupted` branch — drop everything still
        queued from the cancelled reply, keep input metadata and completion
        evidence, then announce the interruption so the live pump stops the
        speaker — and adds the OpenAI-specific step: truncate the assistant's
        audio item on the server to what was actually delivered, so the model
        does not believe the user heard words that never played.
        """
        dropped = 0
        dropped_ms = 0.0
        metadata: list[OutputEvent] = []
        while True:
            try:
                queued = self._recv_queue.get_nowait()
            except queue.Empty:
                break
            dropped += 1
            if isinstance(queued, OutputEvent):
                if isinstance(queued.output, (UserSpeechOutput, ExecutionOutput)) or (
                    isinstance(queued.output, InterruptedOutput)
                    and queued.output.reason == "server_interrupt"
                ):
                    metadata.append(queued)
                elif isinstance(queued.output, AudioOutput):
                    dropped_ms += len(queued.output.audio) * 1000.0 / self._output_rate()
            elif app_config.LIVE_MODE and isinstance(queued, TurnDoneEvent):
                # Preserve metric evidence, never replay a control terminal
                # that the original interruption path dropped.
                metadata.append(OutputEvent(
                    gen=getattr(self, "_turn_gen", 0),
                    output=ExecutionOutput(
                        user_turn_id=queued.user_turn_id,
                        execution_completed=queued.execution_completed,
                    ),
                ))
        for queued in metadata:
            self._recv_queue.put(queued)
        # Anything from the cancelled reply still in flight is now older than
        # what follows; receive() drops it by generation.
        self._turn_gen = getattr(self, "_turn_gen", 0) + 1
        if app_config.LIVE_MODE:
            self._recv_queue.put(OutputEvent(
                gen=self._turn_gen,
                output=InterruptedOutput(
                    reason="server_interrupt", at=time.monotonic(),
                    user_turn_id=user_turn_id,
                ),
            ))
        if item is not None and conn is not None:
            # Best effort: received minus still-queued approximates delivered.
            # HAL's own playback buffer makes this an over-estimate by a few
            # hundred ms; a value past the real length only yields a benign
            # `error` tagged with _TRUNCATE_EVENT_ID.
            self._truncate_item(conn, item, max(0, int(received_ms - dropped_ms)))
        self._first_audio_received = False
        self._turn_done.set()
        logger.info(
            "[realtime] Response interrupted — dropped %d queued output(s), gen=%d",
            dropped, self._turn_gen,
        )

    def _truncate_item(self, conn: RealtimeConnection, item: tuple[str, int], audio_end_ms: int) -> None:
        item_id, content_index = item
        try:
            with self._conn_lock:
                if self._connection is not conn:
                    return
                conn.conversation.item.truncate(
                    item_id=item_id,
                    content_index=content_index,
                    audio_end_ms=audio_end_ms,
                    event_id=_TRUNCATE_EVENT_ID,
                )
            logger.debug("[realtime] Truncated %s at %dms", item_id, audio_end_ms)
        except Exception as e:  # a failed truncate must never end the turn
            logger.warning("[realtime] conversation.item.truncate failed: %s", e)

    # --- Usage ---

    def _log_usage(self, response: Any) -> None:
        """Per-turn token bill (see gemini_live for the reading guide).

        input_tokens is the input CONTEXT billed this turn — it grows as a
        long-lived session accumulates history and should drop right after an
        idle session recycle (orchestrator._mark_turn_start). cached is the
        prompt-cache hit, re-billed at the discounted rate; cached=0 every turn
        means the cache is not hitting (session churn) — that's the cost red
        flag. Grep "[realtime] OpenAI usage" in openai_usage.log.
        """
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        cfg = getattr(self, "_config", None)
        model: str = cfg.model if cfg is not None else ""
        rates = _openai_rates_for(model)
        parts: list[str] = []
        cost = 0.0
        attributed = {"in": 0, "out": 0}
        in_details = getattr(usage, "input_token_details", None)
        out_details = getattr(usage, "output_token_details", None)
        for direction, details in (("in", in_details), ("out", out_details)):
            for key in ("text_tokens", "audio_tokens"):
                tok = getattr(details, key, 0) or 0
                mod = key.split("_", 1)[0].upper()
                attributed[direction] += tok
                c = tok * rates.get((direction, mod), 0.0) / 1_000_000
                cost += c
                parts.append("%s_%s=%d($%.5f)" % (direction, mod.lower(), tok, c))
        # Tokens OpenAI counted but did not tag text/audio (image input) —
        # unpriced here, so est is a floor.
        unattr_in = (getattr(usage, "input_tokens", 0) or 0) - attributed["in"]
        unattr_out = (getattr(usage, "output_tokens", 0) or 0) - attributed["out"]
        cached = getattr(in_details, "cached_tokens", 0) or 0
        cached_details = getattr(in_details, "cached_tokens_details", None)
        cached_audio = getattr(cached_details, "audio_tokens", 0) or 0
        # Untagged cached tokens are text (the system-instruction floor).
        cached_text = (getattr(cached_details, "text_tokens", None)
                       if cached_details is not None else None)
        if cached_text is None:
            cached_text = max(0, cached - cached_audio)
        saving = (
            cached_text * (rates[("in", "TEXT")] - rates[("cached", "TEXT")])
            + cached_audio * (rates[("in", "AUDIO")] - rates[("cached", "AUDIO")])
        ) / 1_000_000
        usage_logger.info(
            "[realtime] OpenAI usage: model=%s %s +unattr(%din/%dout) | "
            "cached=%dtok total=%dtok est_full>=$%.5f est_cached>=$%.5f",
            model, " ".join(parts) or "-", unattr_in, unattr_out, cached,
            getattr(usage, "total_tokens", 0) or 0, cost, max(0.0, cost - saving),
        )

    # --- Receive ---

    def _sync_receive_turn(self, conn: RealtimeConnection) -> bool:
        """Read one full turn from `conn`, put outputs on _recv_queue.

        Iterates on the caller-supplied connection snapshot (not self._connection)
        so a concurrent reconnect that swaps the connection can't be read mid-turn.

        Returns True when the turn ended normally (a `response.done` was seen),
        False when the event iteration ended WITHOUT one — i.e. a clean connection
        close / session recycle. The caller uses this to decide whether to fail the
        turn fast; returning the flag (instead of inspecting `_turn_done`) avoids a
        race where a normal completion is mistaken for a mid-turn drop and a
        spurious TurnDoneEvent ends the NEXT turn.
        """
        # A new receive turn is a new generation; a barge-in bumps it again.
        self._turn_gen = getattr(self, "_turn_gen", 0) + 1
        self._first_audio_received = False
        # Ownership of THIS response: frozen at response.created (or at the
        # first output if that was missed) so a transcription that arrives
        # later can never re-attribute the reply to a newer input.
        response_user_turn_id: str | None = None
        active_response_id: str | None = None
        cancelled_response_id: str | None = None
        execution_interrupted = False
        # The assistant audio item being streamed and how much of it we have
        # received, for the truncate on barge-in.
        active_item: tuple[str, int] | None = None
        received_ms = 0.0
        transcript_chunks = 0

        for event in conn:
            # Liveness for the silent-turn watchdog: a turn busy reasoning or
            # running a tool sends plenty we never queue (rate limits, item
            # lifecycle, transcription) — see VoiceAgentBase.note_server_activity.
            self.note_server_activity()
            etype: str = getattr(event, "type", "")

            match etype:
                # ---- user side -------------------------------------------
                case "input_audio_buffer.speech_started":
                    if active_response_id is not None and not execution_interrupted:
                        # Barge-in. With interrupt_response (API default) the
                        # server cancels the response itself; the local queue
                        # is ours to flush, right now, before the new input
                        # takes over the live key.
                        execution_interrupted = True
                        cancelled_response_id = active_response_id
                        owner = (response_user_turn_id if response_user_turn_id is not None
                                 else getattr(self, "_live_user_turn_id", ""))
                        response_user_turn_id = owner
                        self._handle_interrupt(
                            conn, user_turn_id=owner, item=active_item,
                            received_ms=received_ms,
                        )
                    self._live_user_turn_id = ""
                    self._live_speech_emitted = False
                    self._observe_user_speech()
                    self._remember_user_item(getattr(event, "item_id", None))

                case "input_audio_buffer.speech_stopped":
                    self._speech_ended_at = time.monotonic()
                    self._observe_user_speech(endpoint_at=self._speech_ended_at)

                case "input_audio_buffer.committed":
                    # Manual turns have no speech_started; this is where the
                    # user item id becomes known.
                    item_id = getattr(event, "item_id", None)
                    items: dict[str, tuple[str, str]] = getattr(self, "_user_items", None) or {}
                    if item_id and item_id not in items:
                        self._remember_user_item(item_id)

                case "conversation.item.input_audio_transcription.delta":
                    self._on_input_transcript(
                        getattr(event, "item_id", None), getattr(event, "delta", "") or "",
                        finished=False,
                    )

                case "conversation.item.input_audio_transcription.completed":
                    self._on_input_transcript(
                        getattr(event, "item_id", None),
                        getattr(event, "transcript", "") or "",
                        finished=True,
                    )

                case "conversation.item.input_audio_transcription.failed":
                    logger.warning(
                        "[realtime] Input transcription failed: %s",
                        getattr(event, "error", None),
                    )

                # ---- model side ------------------------------------------
                case "response.created":
                    active_response_id = getattr(getattr(event, "response", None), "id", None) or ""
                    if response_user_turn_id is None:
                        response_user_turn_id = getattr(self, "_live_user_turn_id", "")

                case "response.output_audio.delta":
                    if execution_interrupted and getattr(event, "response_id", None) == cancelled_response_id:
                        continue  # in-flight audio of the cancelled reply
                    if response_user_turn_id is None:
                        response_user_turn_id = getattr(self, "_live_user_turn_id", "")
                    audio = base64_pcm16_to_float32(event.delta)
                    received_ms += len(audio) * 1000.0 / self._output_rate()
                    item_id = getattr(event, "item_id", None)
                    if item_id:
                        active_item = (item_id, getattr(event, "content_index", 0) or 0)
                    if not self._first_audio_received:
                        self._first_audio_received = True
                        self._log_first_audio_latency()
                    self._recv_queue.put(
                        OutputEvent(
                            gen=getattr(self, "_turn_gen", 0),
                            output=AudioOutput(
                                user_turn_id=response_user_turn_id or "",
                                audio=audio,
                            ),
                        )
                    )

                case "response.output_audio_transcript.delta":
                    if execution_interrupted and getattr(event, "response_id", None) == cancelled_response_id:
                        continue
                    if response_user_turn_id is None:
                        response_user_turn_id = getattr(self, "_live_user_turn_id", "")
                    if not transcript_chunks:
                        # First words of a reply: reset the live TTS queue so
                        # this response never plays behind a stale one.
                        self._recv_queue.put(OutputEvent(
                            gen=getattr(self, "_turn_gen", 0),
                            output=InterruptedOutput(
                                reason="output_reset",
                                user_turn_id=response_user_turn_id or "",
                            ),
                        ))
                    transcript_chunks += 1
                    self._recv_queue.put(
                        OutputEvent(
                            gen=getattr(self, "_turn_gen", 0),
                            output=TextOutput(
                                text=event.delta,
                                user_turn_id=response_user_turn_id or "",
                            ),
                        )
                    )

                case "response.output_text.delta":
                    # The session is audio-only (see _build_session), so this
                    # should not fire; if it does, the audio transcript is the
                    # source of truth and emitting both double-speaks the reply.
                    continue

                case "response.function_call_arguments.done":
                    if response_user_turn_id is None:
                        response_user_turn_id = getattr(self, "_live_user_turn_id", "")
                    user_transcript = getattr(self, "_user_transcript", "").strip()
                    self._user_transcript = ""
                    logger.debug(
                        "[realtime] Function call: %s (call_id=%s)", event.name, event.call_id
                    )
                    self._recv_queue.put(
                        OutputEvent(
                            gen=getattr(self, "_turn_gen", 0),
                            output=FunctionCallOutput(
                                user_turn_id=response_user_turn_id or "",
                                name=event.name,
                                arguments=event.arguments,
                                call_id=event.call_id,
                                user_transcript=user_transcript,
                            ),
                        )
                    )

                case "response.done":
                    response = getattr(event, "response", None)
                    status = getattr(response, "status", None)
                    logger.debug("[realtime] Response complete (status=%s)", status)
                    self._log_usage(response)
                    if status == "cancelled" and not execution_interrupted:
                        # Cancelled without a speech_started of ours (a
                        # client response.cancel, or semantic VAD deciding
                        # mid-reply): flush and announce now.
                        execution_interrupted = True
                        owner = (response_user_turn_id if response_user_turn_id is not None
                                 else getattr(self, "_live_user_turn_id", ""))
                        response_user_turn_id = owner
                        self._handle_interrupt(
                            conn, user_turn_id=owner, item=active_item,
                            received_ms=received_ms,
                        )
                    self._first_audio_received = False
                    self._turn_done.set()
                    owner = (response_user_turn_id if response_user_turn_id is not None
                             else getattr(self, "_live_user_turn_id", ""))
                    self._recv_queue.put(TurnDoneEvent(
                        execution_completed=status == "completed" and not execution_interrupted,
                        user_turn_id=owner,
                    ))
                    if response_user_turn_id is None or (
                        getattr(self, "_live_user_turn_id", "") == response_user_turn_id
                    ):
                        # This input has been answered; a later unsolicited
                        # reply must not be attributed to it.
                        self._live_user_turn_id = ""
                        self._live_speech_emitted = False
                        self._user_transcript = ""
                    return True

                case "error":
                    err = getattr(event, "error", None)
                    code = getattr(err, "code", None) or ""
                    if code in _BENIGN_ERROR_CODES or getattr(err, "event_id", None) == _TRUNCATE_EVENT_ID:
                        logger.info("[realtime] Realtime API notice (%s): %s", code, getattr(err, "message", err))
                        continue
                    logger.error("[realtime] Realtime API error: %s", err)
                    raise OpenAIRealtimeError(f"Realtime API error: {err}")

                case _:
                    # session.*, rate_limits.updated, conversation.item.*,
                    # response.output_item.*, response.content_part.*,
                    # response.output_audio.done, ... — lifecycle only.
                    pass

        # Iteration ended without a response.done — connection closed cleanly.
        return False

    def _log_first_audio_latency(self) -> None:
        now = time.monotonic()
        speech_ended_at = getattr(self, "_speech_ended_at", None)
        commit_sent_at = getattr(self, "_commit_sent_at", None)
        if speech_ended_at is not None:
            if commit_sent_at is not None:
                logger.info(
                    "[realtime] Response latency: %.0fms (speech_end->first_audio; "
                    "commit_sent->first_audio=%.0fms)",
                    (now - speech_ended_at) * 1000, (now - commit_sent_at) * 1000,
                )
            else:
                logger.info(
                    "[realtime] Response latency: %.0fms (speech_end->first_audio; server VAD)",
                    (now - speech_ended_at) * 1000,
                )
        self._speech_ended_at = None
        self._commit_sent_at = None

    # --- Reconnect ---

    def _ensure_connected(self) -> None:
        """Reconnect if not connected. Throttled by an exponential backoff that grows
        on consecutive failures (reset to base on success)."""
        if self._stop_event.is_set():
            return
        if self._connected.is_set():
            return
        now: float = time.monotonic()
        if now - self._last_reconnect_at < self._reconnect_backoff:
            return
        self._last_reconnect_at = now
        self._reconnect()

    def _reconnect(self) -> None:
        if self._stop_event.is_set():
            return
        with self._conn_lock:
            if self._stop_event.is_set():
                return
            # Another thread may have reconnected while we waited for the lock —
            # don't tear a healthy connection back down.
            if self._connected.is_set():
                return
            self._turn_done.set()  # unblock any waiting commit
            try:
                logger.info("[realtime] Reconnecting...")
                self._sync_disconnect()
                self._sync_connect()
                self._connected.set()
                self._reconnect_backoff = self._reconnect_delay_s  # success → reset
            except Exception as e:
                self._reconnect_backoff = min(
                    self._reconnect_backoff * 2, self._reconnect_backoff_max
                )
                logger.warning(
                    "[realtime] Reconnect failed: %s — next retry in ~%.0fs",
                    e, self._reconnect_backoff,
                )

    def _fail_fast_turn(self, reason: str) -> None:
        """End the current turn immediately on a recv error.

        Without this, any backend failure (a Realtime API `error` event, a
        dropped socket, an unexpected exception) leaves the consumer blocked in
        receive() for the full REALTIME_RECV_QUEUE_TIMEOUT_S before the turn
        falls back to the main agent. Pushing a TurnDoneEvent unblocks receive()
        now so the main agent answers without the dead-air wait. A benign idle
        close is not an error here — it ends the `for event in conn` iteration
        cleanly — so only real errors reach this path. Only fires while a turn is
        actually awaiting output (_turn_done clear); a stray sentinel between
        turns would just be dropped by flush_output() at the next turn start.
        Late output from a successful reconnect is likewise flushed next turn.
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

    def _drop_connection(self, conn: RealtimeConnection | None) -> None:
        """Mark the connection dead — only if `conn` is still the current one.

        Both loops call this on error; the identity check stops one thread from
        nulling a connection the other thread just re-established.
        """
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
                event: AgentInputEvent = self._send_queue.get(timeout=self._queue_poll_s)
            except queue.Empty:
                continue

            for attempt in range(self._max_retries):
                self._ensure_connected()
                if self._stop_event.is_set():
                    break
                if not self._connected.is_set():
                    logger.debug("[realtime] Not connected, skipping attempt %d/%d", attempt + 1, self._max_retries)
                    continue
                conn: RealtimeConnection | None = self._connection
                try:
                    if isinstance(event, AudioCommitEvent):
                        self._sync_commit(event.queued_at)
                    elif isinstance(event, InputEvent) and event.input is not None:
                        self._sync_send_input(event.input)
                    break  # Success
                except Exception as e:
                    if self._stop_event.is_set():
                        break
                    logger.exception("[realtime] Send failed (attempt %d/%d): %s", attempt + 1, self._max_retries, e)
                    self._drop_connection(conn)

    @override
    def _recv_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._connected.is_set():
                # Proactively reconnect (throttled) so the session self-heals while
                # disconnected even with no audio flowing — without this, once
                # voice_service stops feeding audio (available=False after a dropped
                # session) nothing would ever trigger a reconnect. Mirrors gemini_live.
                self._ensure_connected()
                if not self._connected.is_set():
                    self._connected.wait(timeout=self._queue_poll_s)
                continue

            for attempt in range(self._max_retries):
                self._ensure_connected()
                if self._stop_event.is_set():
                    break
                with self._conn_lock:
                    conn: RealtimeConnection | None = (
                        self._connection if self._connected.is_set() else None
                    )
                if conn is None:
                    logger.debug("[realtime] Not connected, skipping attempt %d/%d", attempt + 1, self._max_retries)
                    continue
                try:
                    completed: bool = self._sync_receive_turn(conn)
                    if self._stop_event.is_set():
                        break
                    # `completed` is False when the event iteration ended WITHOUT a
                    # response.done (a clean connection close / session recycle). If
                    # that lands mid-turn the committed turn is lost, so end it now
                    # instead of waiting out the receive timeout. Do NOT fail-fast on
                    # a normal completion: that would race the next turn's commit and
                    # could push a spurious TurnDoneEvent that ends turn N+1 empty.
                    if not completed:
                        self._fail_fast_turn("connection closed mid-turn")
                    break  # Success
                except OpenAIRealtimeError as e:
                    if self._stop_event.is_set():
                        break
                    logger.warning("[realtime] Recv failed (attempt %d/%d): %s", attempt + 1, self._max_retries, e)
                    self._fail_fast_turn("api error")
                    self._drop_connection(conn)
                except Exception as e:
                    if self._stop_event.is_set():
                        break
                    logger.exception("[realtime] Unexpected recv error (attempt %d/%d): %s", attempt + 1, self._max_retries, e)
                    self._fail_fast_turn("unexpected")
                    self._drop_connection(conn)
