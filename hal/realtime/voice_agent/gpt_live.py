"""GPT-Live (OpenAI `/v1/live`, `gpt-live-1`) voice agent — queue-based threading, sync.

GPT-Live is NOT the Realtime API. It is a full-duplex model behind a different
WebSocket (`client.live.connect()`, `session.start` → `session.started`) whose
wire has none of the things the VoiceAgentBase contract was built around, so
this adapter SYNTHESIZES them and says so in each place:

  - No turn boundary (no `response.done` / `turn_complete`), and output audio
    is streamed CONTINUOUSLY, silence included (device-measured 2026-09-17:
    ~100 ms deltas for the whole session, speech-level for the reply only). So
    silent deltas are dropped at the door (`output_silence_dbfs`) and a reply
    is over when no SPEECH-level audio or transcript has arrived for
    `turn_gap_ms`; a watchdog thread fires the TurnDoneEvent. A burst that
    never carried a transcript is a backchannel, not an answer. See
    `_on_output` / `_fire_boundary`.
  - No VAD events, no interruption event. User speech is observed ONLY through
    `session.input_transcript.delta` fragments (so `UserSpeechOutput` never
    carries a VAD `endpoint_at`). A fragment arriving while the model is
    speaking marks a barge-in candidate; if the model then falls silent for
    `interrupt_gap_ms` the reply counts as interrupted (queue drained,
    `InterruptedOutput(server_interrupt)`), if it keeps talking it was a
    backchannel. See `_on_input_transcript` / `_on_output`.
  - No tools at the Live layer. Two delegation modes (`GPTLiveConfig.delegation`):
      * CLIENT: when the model decides the request needs the backend it emits
        `session.delegation.created` (metadata only), which this adapter turns
        into the `delegate_to_main` FunctionCallOutput the orchestrator already
        handles; the task text is the accumulated input transcript.
      * RESPONSES (default when `web_search` is on): an OpenAI-hosted Responses
        backend does the delegated work with `web_search` for live facts and
        calls OUR `delegate_to_main` function for anything that needs the
        device. Its calls arrive wrapped in `response.event` and are answered
        with `response.item.create` + `response.create`; Live hands it the
        conversation context itself, so nothing is reconstructed here.
    `express_emotion`, `reject_turn`, `end_conversation` and `look` cannot exist
    on this provider and are ignored with one log line each.
  - Feedback to the model goes through `session.thinking.append` (silent
    context, ≤500 tokens): the delegate ack, `[TURN CONTEXT]`, and the main
    agent's spoken reply (`[TTS HISTORY]`, which also closes the pending
    delegation).
  - Billing is per session-minute ($0.05/min, billed per second), not per token:
    `session.usage.updated` lines go to gptlive_usage.log. An OPEN idle session
    costs money — the orchestrator parks (closes) it after
    REALTIME_GPTLIVE_IDLE_PARK_S of inactivity and reconnects on the next turn.

Wire: the same base URL as OpenAI Realtime (`<llm_base_url>/ws/openai` through
the campaign-api proxy) — the SDK appends `/live/sessions` to it, so the proxy
serves `…/ws/openai/realtime` for the Realtime API and `…/ws/openai/live/sessions`
for this one. Until the proxy route exists the connect 404s and the reconnect
backoff (2 s → 60 s) keeps retrying; nothing else needs to change when it lands.
"""

import json
import logging
import queue
import threading
import time
from typing import Any, override
from uuid import uuid4

import numpy as np
from openai import OpenAI
from openai.resources.live.live import LiveConnection
from websockets.exceptions import ConnectionClosed

from hal import config as app_config
from hal.realtime.config import GPTLiveConfig
from hal.realtime.constants import RESOURCES_DIR
from hal.realtime.exceptions import GPTLiveError
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
from hal.realtime.utils import base64_pcm16_to_float32, float32_to_base64_pcm16
from hal.realtime.voice_agent.base import VoiceAgentBase

logger = logging.getLogger(__name__)
# Per-session usage lines → gptlive_usage.log (child logger configured in
# server_support/log_setup.py with propagate=False).
usage_logger = logging.getLogger("hal.realtime.usage.gptlive")

# developers.openai.com/api/docs/models/gpt-live-1 (verified 2026-09-16): voice
# sessions are $0.05 per minute, billed per second; backend work is separate.
_GPTLIVE_USD_PER_MINUTE: float = 0.05

# The only orchestrator tool this provider can honour. Everything else in the
# tool list is a Realtime-API function tool; GPT-Live has no Live-layer tools.
DELEGATE_TOOL_NAME: str = "delegate_to_main"

# Client event_id prefixes stamped on our own commands. A server `error` that
# names one of them means a command of OURS was rejected (over-long context,
# audio before session.started, …) — log it and carry on. Only an error with
# no client id, or one on session.start, means the session itself is broken.
_EVT_START: str = "hal-start"
_EVT_AUDIO: str = "hal-audio"
_EVT_SILENCE: str = "hal-silence"
_EVT_CONTEXT: str = "hal-context"
_EVT_DELEGATION: str = "hal-delegation"

# `session.*.append` content is capped at 500 tokens by the API; clip by chars
# (≈3.5 chars/token for mixed English/Vietnamese) rather than let the append be
# rejected outright and lose the whole context line.
_APPEND_MAX_CHARS: int = 1600
# Bound on remembered delegations so a session where the main agent never
# answers cannot grow the map without limit.
_MAX_PENDING_DELEGATIONS: int = 8
_BACKEND_PROMPT_PATH = RESOURCES_DIR / "system_prompt_gptlive_backend.md"
# What the Responses backend gets back for a delegated call. The device speaks
# the main agent's answer itself (HAL TTS), so the backend must not answer too.
_BACKEND_DELEGATED_OUTPUT: str = json.dumps({
    "result": "delegated",
    "note": "The device will speak the main agent's answer aloud itself. Do not "
            "answer the request; reply with at most a two-word acknowledgment.",
})
# A delegation can land before the sentence is complete in the transcript (BFF
# integration doc §6). Forward it only once the input transcript has been quiet
# for this long, so "turn off" is not handed to the main agent while "the light"
# is still on its way. The hard cap is `delegation_wait_ms`.
_DELEGATION_SETTLE_S: float = 0.25
# Instructions are capped at 16,384 tokens by the API; warn well before a
# session.start would be rejected for it (≈3.5 chars/token).
_INSTRUCTIONS_WARN_CHARS: int = 50_000
# BFF close codes that mean "do not hammer the relay" (integration doc §8):
# 4001 no key, 4002 GPT-Live not configured on this BFF, 4029 device over its
# usage limit. The session is not coming back until config/limits change, so the
# reconnect backoff jumps straight to its ceiling instead of ramping 2 s → 60 s.
_NO_RETRY_CLOSE_CODES: dict[int, str] = {
    4001: "no API key on the upgrade (device lobster key missing)",
    4002: "GPT-Live is not configured on this BFF",
    4029: "device over its GPT-Live usage limit",
}


def _clip(text: str) -> str:
    return text if len(text) <= _APPEND_MAX_CHARS else text[: _APPEND_MAX_CHARS - 1] + "…"


def _public_key(owner: str) -> str:
    """The user-turn key as the consumer sees it. The adapter needs an input key
    internally in both modes (reply attribution, boundaries), but like the other
    providers it only PUBLISHES one in live mode; the turn path never reads it."""
    return owner if app_config.LIVE_MODE else ""


class GPTLiveAgent(VoiceAgentBase):
    """GPT-Live provider (`realtime.provider = gptlive`).

    Base-class hooks left at their defaults, with the reason:
      - `end_turn()` no-op: there is no `_turn_done` gate here — nothing waits
        for a provider turn end before the next send (no `response.create`).
      - `requires_fresh_session` False: a delegation the client never answers
        does not make the session refuse input (unlike Gemini's 1008).
      - `output_sample_rate` == `sample_rate`: a Live WebSocket has ONE PCM
        format for both directions.
    """

    def __init__(
        self,
        config: GPTLiveConfig,
        tools: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(tools=tools)
        self._config: GPTLiveConfig = config
        client_kwargs: dict[str, Any] = {"api_key": config.api_key}
        if config.base_url:
            client_kwargs["base_url"] = config.base_url
        self._client: OpenAI = OpenAI(**client_kwargs)
        self._connection: LiveConnection | None = None
        # Serializes connection swaps and writes across the send/recv threads;
        # the blocking recv iteration runs outside it on a snapshot.
        self._conn_lock: threading.RLock = threading.RLock()
        # Turn/ownership state is touched by the recv thread AND the boundary
        # watchdog, so it has its own lock.
        self._state_lock: threading.RLock = threading.RLock()
        # Set by the recv loop on `session.started`; sends wait for it because
        # a command before that is rejected by the server.
        self._session_started: threading.Event = threading.Event()
        # Set on `session.closed`: a graceful close keeps reading until it lands
        # so the relay can confirm the final usage (BFF integration doc §7).
        self._session_closed: threading.Event = threading.Event()
        self._session_id: str = ""
        self._request_id: str = ""
        self._session_started_once: bool = False
        self._reconnect_delay_s: float = config.reconnect_delay_s
        self._max_retries: int = config.max_retries
        self._queue_poll_s: float = config.queue_poll_s
        self._last_reconnect_at: float = 0.0
        self._reconnect_backoff: float = config.reconnect_delay_s
        self._reconnect_backoff_max: float = 60.0
        self._turn_gap_s: float = config.turn_gap_ms / 1000.0
        self._interrupt_gap_s: float = config.interrupt_gap_ms / 1000.0
        self._input_gap_ms: int = config.input_gap_ms
        self._delegation_wait_s: float = config.delegation_wait_ms / 1000.0
        # --- turn state (guarded by _state_lock) ---
        self._turn_gen: int = 0
        self._live_user_turn_id: str = ""
        self._live_speech_emitted: bool = False
        self._user_transcript: str = ""
        self._input_answered: bool = False
        self._last_input_end_ms: int | None = None
        self._last_input_at: float | None = None
        self._output_active: bool = False
        self._response_user_turn_id: str = ""
        self._output_transcript_chunks: int = 0
        self._silent_deltas_dropped: int = 0
        self._first_audio_received: bool = False
        self._overlap_pending: bool = False
        self._overlap_at: float = 0.0
        self._boundary_deadline: float | None = None
        self._awaiting_reply: bool = False
        self._pending_delegations: dict[str, str] = {}
        self._deferred_delegation: tuple[str, str, float] | None = None
        # Responses mode: function calls the backend made that we still owe a
        # result (call_id → delegation_id).
        self._pending_function_calls: dict[str, str] = {}
        # Delegations the backend is still working on (delegation_id → owner).
        # While one is in flight for an input, whatever the model says is a
        # filler ("Mmm.", "let me check") — not the answer to that input.
        self._backend_busy: dict[str, str] = {}
        self._usage_seconds: float = 0.0
        self._usage_ratio: float | None = None
        # --- boundary watchdog ---
        self._watchdog_stop: threading.Event = threading.Event()
        self._watchdog_thread: threading.Thread | None = None
        self._warned: set[str] = set()
        unsupported = sorted(
            t.get("name", "?") for t in self._tools if t.get("name") != DELEGATE_TOOL_NAME
        )
        if unsupported:
            logger.info(
                "[realtime] GPT-Live has no Live-layer tools — %s unavailable on this "
                "provider (only %s, via client delegation)",
                unsupported, DELEGATE_TOOL_NAME,
            )

    @property
    @override
    def sample_rate(self) -> int:
        return self._config.sample_rate

    # --- Session config ---

    def _build_session(self) -> dict[str, Any]:
        """The `session.start` payload (`openai.types.live.SessionConfig`)."""
        if len(self._config.instructions) > _INSTRUCTIONS_WARN_CHARS:
            logger.warning(
                "[realtime] GPT-Live instructions are %d chars — the API caps them at "
                "16,384 tokens; session.start may be rejected",
                len(self._config.instructions),
            )
        return {
            "model": self._config.model,
            # Frontend prompt: voice, silence/interruption policy, WHEN to hand
            # off (system_prompt_gptlive.md). Business rules live in the main
            # agent, which is the backend here.
            "instructions": self._config.instructions,
            "audio": {
                # One format for both directions; only 16000 / 24000 Hz PCM.
                "format": {"type": "audio/pcm", "rate": self._config.sample_rate},
                "output": {"voice": self._config.voice.value},
            },
            "delegation": self._delegation_config(),
        }

    def _delegation_config(self) -> dict[str, Any]:
        """Who does the delegated work (immutable for the session).

        Client: the model emits session.delegation.created and THIS process
        (→ the main agent) does the work. Responses: an OpenAI-hosted backend
        with `web_search` for live facts, plus our delegate_to_main as a
        function tool so device work still reaches the main agent.
        """
        if not self._config.responses_mode:
            return {"type": "client"}
        tools: list[dict[str, Any]] = []
        if self._config.web_search:
            tools.append({"type": "web_search"})
        for tool in self._tools:
            if tool.get("name") == DELEGATE_TOOL_NAME:
                tools.append({
                    "type": "function",
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters"),
                })
        return {
            "type": "responses",
            "responses": {
                "model": self._config.backend_model,
                "instructions": self._backend_instructions(),
                "tools": tools,
                "tool_choice": "auto",
                "parallel_tool_calls": False,
            },
        }

    def _backend_instructions(self) -> str:
        from hal.realtime.context_manager.base import ContextManagerBase
        lang = (self._config.language or "en").split("-", 1)[0].lower()
        name = ContextManagerBase.LANGUAGE_NAMES.get(self._config.language or "", None) \
            or ContextManagerBase.LANGUAGE_NAMES.get(lang, "English")
        try:
            template = _BACKEND_PROMPT_PATH.read_text(encoding="utf-8")
        except OSError:
            template = "Do the delegated task and return a short spoken result in {language}."
        return template.replace("{language}", name)

    # --- Sync internals ---

    def _reset_turn_state(self) -> None:
        with self._state_lock:
            self._live_user_turn_id = ""
            self._live_speech_emitted = False
            self._user_transcript = ""
            self._input_answered = False
            self._last_input_end_ms = None
            self._last_input_at = None
            self._output_active = False
            self._response_user_turn_id = ""
            self._output_transcript_chunks = 0
            self._silent_deltas_dropped = 0
            self._first_audio_received = False
            self._overlap_pending = False
            self._boundary_deadline = None
            self._awaiting_reply = False
            self._pending_delegations = {}
            self._deferred_delegation = None
            self._pending_function_calls = {}
            self._backend_busy = {}

    def _sync_connect(self) -> None:
        # A reopened session must not inherit input ownership or delegations.
        self._reset_turn_state()
        self._session_started.clear()
        self._session_closed.clear()
        self._session_id = ""
        # Correlation id: the BFF prefixes every usage row of the session with
        # it (`<id>:voice-N`), so this log line joins lobster_usage to HAL's log.
        self._request_id = "hal-" + uuid4().hex[:12]
        logger.info(
            "Connecting to GPT-Live (base_url=%s, model=%s, x-request-id=%s)",
            self._config.base_url or "(SDK default)",
            self._config.model,
            self._request_id,
        )
        # No on_reconnecting → the SDK never reconnects on its own; the
        # send/recv loops below own recovery (backoff, fail-fast) like the
        # other providers.
        conn: LiveConnection = self._client.live.connect(
            extra_headers={"x-request-id": self._request_id},
        ).enter()
        try:
            conn.session.start(session=self._build_session(), event_id=_EVT_START)
        except Exception:
            conn.close()
            raise
        self._connection = conn
        logger.info("[realtime] GPT-Live session.start sent (voice=%s)", self._config.voice)

    def _sync_disconnect(self, *, wait_closed: bool = False) -> None:
        """Close the session. `wait_closed` keeps the socket open until the
        server's `session.closed` lands (or `close_timeout_s`), which is what
        lets the relay confirm the final usage instead of billing from its own
        clock. Only the owner-side teardown may wait: the recv thread itself
        (reconnect path) is the one that would have to read that event."""
        conn = self._connection
        self._connection = None
        self._session_started.clear()
        if conn is None:
            return
        logger.info("[realtime] Disconnecting from GPT-Live (usage so far %.1fs)", self._usage_seconds)
        try:
            # Graceful: the server finalizes and answers session.closed with
            # the final usage before the socket goes.
            conn.session.close()
            if wait_closed and self._session_started_once and not self._session_closed.wait(
                timeout=self._config.close_timeout_s
            ):
                logger.info("[realtime] GPT-Live session.closed not seen within %.0fs — closing socket",
                            self._config.close_timeout_s)
        except Exception:
            pass
        try:
            conn.close()
        except Exception as e:
            logger.debug("[realtime] GPT-Live close: %s", e)

    def _warn_once(self, key: str, msg: str, *args: Any) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warning(msg, *args)

    def _ready(self) -> bool:
        """Block (send thread) until session.started, or give up on this send."""
        if self._session_started.wait(timeout=self._config.start_timeout_s):
            return True
        logger.warning("[realtime] GPT-Live session not started within %.0fs — dropping send",
                       self._config.start_timeout_s)
        return False

    def _sync_send_input(self, input: InputBase) -> None:
        if not self._ready():
            return
        with self._conn_lock:
            conn = self._connection
            if conn is None:
                return
            if isinstance(input, AudioInput):
                conn.session.input_audio.append(
                    audio=float32_to_base64_pcm16(input.audio), event_id=_EVT_AUDIO,
                )
            elif isinstance(input, TextInput):
                self._send_context(conn, input.text)
            elif isinstance(input, ImageInput):
                # Image input is unsupported by gpt-live-1 (model card). `look`
                # is registered for Gemini only, so this is a stray frame.
                self._warn_once("image", "[realtime] GPT-Live has no image input — frame dropped")
            elif isinstance(input, FunctionCallResultInput):
                self._send_delegation_result(conn, input)

    def _send_context(self, conn: LiveConnection, text: str) -> None:
        """Context injection → `session.thinking.append` (silent).

        `[TTS HISTORY] …` is the main agent's spoken reply to a delegated
        request: it is attached to the newest pending delegation, which closes
        it, so the model knows the user has been answered and by whom. Other
        context (`[TURN CONTEXT]`, speaker corrections) is general session
        context (delegation_id=null).
        """
        delegation_id: str | None = None
        # Non-null delegation ids are rejected with Responses delegation: there
        # the backend owns the task and context is session-wide only.
        if text.startswith("[TTS HISTORY") and not self._config.responses_mode:
            with self._state_lock:
                if self._pending_delegations:
                    delegation_id = next(reversed(self._pending_delegations))
                    del self._pending_delegations[delegation_id]
        conn.session.thinking.append(
            content=_clip(text), delegation_id=delegation_id, event_id=_EVT_CONTEXT,
        )

    def _send_delegation_result(self, conn: LiveConnection, result: FunctionCallResultInput) -> None:
        """The orchestrator's tool result for a delegation → silent context.

        `{"result": "delegated"}` means the main agent owns the request and will
        speak its own reply through the device; the model must neither answer
        it nor announce the handoff again. Anything else is a failed handoff
        (e.g. an empty message), so the model gets to answer directly.
        """
        did = result.call_id
        try:
            parsed: Any = json.loads(result.output)
        except (json.JSONDecodeError, TypeError):
            parsed = {"result": result.output}
        delegated = isinstance(parsed, dict) and parsed.get("result") == "delegated"
        with self._state_lock:
            backend_call = self._pending_function_calls.pop(did, None)
        if backend_call is not None:
            # A function call the Responses backend made: hand the result back
            # and let the backend finish its turn (Live speaks whatever it
            # says — the note keeps that to an acknowledgment).
            conn.response.item.create(
                item={
                    "type": "function_call_output",
                    "call_id": did,
                    "output": _BACKEND_DELEGATED_OUTPUT if delegated else result.output,
                },
                event_id=_EVT_DELEGATION,
            )
            conn.response.create(event_id=_EVT_DELEGATION)
            return
        with self._state_lock:
            known = did in self._pending_delegations
            if known and not delegated:
                del self._pending_delegations[did]
        if not known:
            # An ack for a tool this provider never emitted (express_emotion,
            # reject_turn, …) — nothing on the wire to answer.
            logger.debug("[realtime] Ignoring result for unknown call %s", did)
            return
        if delegated:
            content = (
                "The device's main agent has taken this request and will speak "
                "its own answer to the user. Do not answer it yourself and do "
                "not mention the handoff again; keep listening."
            )
        else:
            content = (
                f"The handoff to the main agent failed ({json.dumps(parsed)[:300]}). "
                "Answer the user directly if you can, or tell them briefly that it did not work."
            )
        conn.session.thinking.append(content=content, delegation_id=did, event_id=_EVT_DELEGATION)

    def _sync_commit(self) -> None:
        """End of a client-bracketed turn (turn path). Live has no commit: the
        model decides when the user is done. Append a short silence so it can
        hear the utterance is over instead of waiting for room noise to tell it;
        in LIVE mode the microphone keeps streaming and nothing is needed."""
        with self._state_lock:
            self._awaiting_reply = True
        if app_config.LIVE_MODE or self._config.commit_silence_ms <= 0:
            return
        if not self._ready():
            return
        with self._conn_lock:
            conn = self._connection
            if conn is None:
                return
            n = self._config.sample_rate * self._config.commit_silence_ms // 1000
            conn.session.input_audio.append(
                audio=float32_to_base64_pcm16(np.zeros(n, dtype=np.float32)),
                event_id=_EVT_SILENCE,
            )

    # --- Live-mode input ownership ---

    def _observe_user_speech(self, *, transcript: str = "", transcript_finished: bool = False) -> None:
        """Publish one input key. Never carries a VAD endpoint: GPT-Live sends
        no speech_started/stopped, so `method` is always provider_transcript."""
        if not app_config.LIVE_MODE:
            return
        with self._state_lock:
            if transcript_finished and not transcript and not self._live_user_turn_id:
                return  # completion-only metadata cannot invent an input turn
            if not self._live_user_turn_id:
                self._live_user_turn_id = "gptlive-" + uuid4().hex
                self._live_speech_emitted = False
            if self._live_speech_emitted and not transcript and not transcript_finished:
                return
            self._live_speech_emitted = True
            turn_id = self._live_user_turn_id
            gen = self._turn_gen
        self._recv_queue.put(OutputEvent(
            gen=gen,
            output=UserSpeechOutput(
                turn_id=turn_id,
                transcript=transcript,
                transcript_finished=transcript_finished,
                user_turn_id=turn_id,
                endpoint_at=None,
                method="provider_transcript",
            ),
        ))

    def _on_input_transcript(self, delta: str, start_ms: int | None, end_ms: int | None) -> None:
        """A fragment of what the user said. Decides whether it opens a NEW input
        turn, and whether it lands on top of a reply in progress (barge-in
        candidate — see `_on_output` / `_fire_boundary` for the verdict)."""
        now = time.monotonic()
        with self._state_lock:
            gap_new = (
                self._last_input_end_ms is not None and start_ms is not None
                and start_ms - self._last_input_end_ms > self._input_gap_ms
            )
            new_turn = not self._live_user_turn_id or self._input_answered or gap_new
            if self._output_active:
                # The user is talking while the model speaks. Whether that is a
                # barge-in or a backchannel is decided by what the model does
                # next (stops → interrupted, keeps going → backchannel).
                if not self._overlap_pending:
                    self._overlap_pending = True
                    self._overlap_at = now
                deadline = now + self._interrupt_gap_s
                if self._boundary_deadline is None or deadline < self._boundary_deadline:
                    self._boundary_deadline = deadline
            if new_turn:
                self._live_user_turn_id = "gptlive-" + uuid4().hex
                self._live_speech_emitted = False
                self._user_transcript = ""
                self._input_answered = False
            if end_ms is not None:
                self._last_input_end_ms = end_ms
            self._last_input_at = now
            self._user_transcript += delta
        if delta:
            logger.info("[realtime] <<< user said: %r", delta)
        self._observe_user_speech(transcript=delta)

    # --- Output / turn boundary ---

    def _on_output(self, *, audio: str | None = None, text: str | None = None) -> None:
        pcm = None
        if audio is not None:
            pcm = base64_pcm16_to_float32(audio)
            # The stream carries silence between and around replies; a silent
            # delta is neither playback material nor evidence of speaking.
            rms_db = 20 * np.log10(float(np.sqrt(np.mean(pcm * pcm))) + 1e-9) if len(pcm) else -200.0
            if rms_db < self._config.output_silence_dbfs:
                with self._state_lock:
                    self._silent_deltas_dropped += 1
                return
        self.note_server_activity()
        now = time.monotonic()
        finished_only = False
        with self._state_lock:
            if not self._output_active:
                # A new reply burst is a new generation, owned by the input it
                # answers (or by nobody, for an unsolicited remark).
                self._output_active = True
                self._turn_gen += 1
                self._output_transcript_chunks = 0
                self._first_audio_received = False
                self._awaiting_reply = False
                if self._live_user_turn_id and not self._input_answered:
                    self._response_user_turn_id = self._live_user_turn_id
                else:
                    self._response_user_turn_id = ""
            if text is not None and self._output_transcript_chunks == 0 and (
                self._response_user_turn_id and not self._input_answered
            ):
                # First WORDS of the reply: the input is complete and answered.
                # (Audio alone can be a backchannel, so this waits for text.)
                self._input_answered = True
                finished_only = True
            if self._overlap_pending and now - self._overlap_at > self._interrupt_gap_s:
                # The model kept talking well past the user's words: backchannel.
                self._overlap_pending = False
            # Silence is measured from the LAST speech-level output either way.
            # While the overlap is still pending the window is the short one —
            # the model stopping within it is the interruption — but continuous
            # output keeps pushing the deadline, so a model that talks straight
            # through the user's words is never cut off by us.
            self._boundary_deadline = now + (
                self._interrupt_gap_s if self._overlap_pending else self._turn_gap_s
            )
            owner = _public_key(self._response_user_turn_id)
            gen = self._turn_gen
            first_audio = pcm is not None and not self._first_audio_received
            if first_audio:
                self._first_audio_received = True
            last_input_at = self._last_input_at
            chunk_index = self._output_transcript_chunks
            if text is not None:
                self._output_transcript_chunks += 1
        if finished_only:
            self._observe_user_speech(transcript_finished=True)
        if pcm is not None:
            if first_audio and last_input_at is not None:
                logger.info(
                    "[realtime] Response latency: %.0fms (last_input_transcript->first_audio; full duplex)",
                    (now - last_input_at) * 1000,
                )
            self._recv_queue.put(OutputEvent(gen=gen, output=AudioOutput(user_turn_id=owner, audio=pcm)))
        if text is not None:
            if chunk_index == 0:
                # First words of a reply: reset the live TTS queue so this reply
                # never plays behind a stale one (same as the other providers).
                self._recv_queue.put(OutputEvent(
                    gen=gen, output=InterruptedOutput(reason="output_reset", user_turn_id=owner),
                ))
            self._recv_queue.put(OutputEvent(gen=gen, output=TextOutput(text=text, user_turn_id=owner)))

    def _fire_boundary(self, *, reason: str = "gap") -> bool:
        """Synthesize the provider turn end the wire never sends.

        Called by the watchdog when output has been quiet for `turn_gap_ms`
        (or `interrupt_gap_ms` after the user spoke over the reply), and by
        `_fail_fast_turn` on a transport error. Returns True when a reply was
        actually closed.
        """
        with self._state_lock:
            if not self._output_active:
                self._boundary_deadline = None
                return False
            owner = self._response_user_turn_id
            interrupted = self._overlap_pending
            # Words spoken while the backend is still working on this input
            # are a filler, not its answer: the key stays open for the reply
            # that follows the backend's result.
            filler = bool(owner) and owner in self._backend_busy.values()
            spoke = self._output_transcript_chunks > 0 and not filler
            dropped = self._silent_deltas_dropped
            self._output_active = False
            self._overlap_pending = False
            self._boundary_deadline = None
            self._first_audio_received = False
            self._output_transcript_chunks = 0
            self._silent_deltas_dropped = 0
            if owner and spoke and self._live_user_turn_id == owner:
                # This input has been answered; a later unsolicited remark must
                # not be attributed to it.
                self._live_user_turn_id = ""
                self._live_speech_emitted = False
            elif owner and not spoke and self._live_user_turn_id == owner:
                # Audio without words (a backchannel, a breath): the question is
                # still open and the next words will belong to it.
                self._input_answered = False
        if interrupted:
            self._drain_for_interrupt(_public_key(owner))
        self._recv_queue.put(TurnDoneEvent(
            execution_completed=(reason == "gap" and not interrupted and spoke),
            user_turn_id=_public_key(owner),
        ))
        logger.info("[realtime] GPT-Live turn boundary (%s%s%s) owner=%s silent_deltas_dropped=%d",
                    reason, ", interrupted" if interrupted else "",
                    "" if spoke else (", filler while backend busy" if filler else ", no words"),
                    owner[-6:] or "-", dropped)
        return True

    def _drain_for_interrupt(self, owner: str) -> None:
        """The model yielded to the user: drop what is still queued from the
        abandoned reply, keep input metadata and completion evidence, bump the
        generation and announce the interruption (same shape as gemini_live)."""
        dropped = 0
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
            elif app_config.LIVE_MODE and isinstance(queued, TurnDoneEvent):
                metadata.append(OutputEvent(
                    gen=self._turn_gen,
                    output=ExecutionOutput(
                        user_turn_id=queued.user_turn_id,
                        execution_completed=queued.execution_completed,
                    ),
                ))
        for queued in metadata:
            self._recv_queue.put(queued)
        with self._state_lock:
            self._turn_gen += 1
            gen = self._turn_gen
        if app_config.LIVE_MODE:
            self._recv_queue.put(OutputEvent(
                gen=gen,
                output=InterruptedOutput(reason="server_interrupt", at=time.monotonic(), user_turn_id=owner),
            ))
        logger.info("[realtime] Reply interrupted by the user — dropped %d queued output(s), gen=%d", dropped, gen)

    # --- Delegation ---

    def _on_delegation(self, delegation_id: str, target: str) -> None:
        """`session.delegation.created` → the orchestrator's `delegate_to_main`.

        The event carries no task text; the request is the input transcript
        accumulated for the current user turn. If transcription has not caught
        up yet (empty), the forward is deferred up to `delegation_wait_ms` and
        flushed by the watchdog — see `_flush_deferred_delegation`.
        """
        if target != "client":
            # The Responses backend has the task; what comes back arrives as
            # response.event envelopes (function calls, usage) — see
            # _on_backend_event. Nothing to reconstruct here.
            with self._state_lock:
                self._backend_busy[delegation_id] = self._live_user_turn_id or self._response_user_turn_id
                while len(self._backend_busy) > _MAX_PENDING_DELEGATIONS:
                    del self._backend_busy[next(iter(self._backend_busy))]
            logger.info("[realtime] Delegation %s handed to the %s backend", delegation_id, target)
            return
        with self._state_lock:
            owner = self._live_user_turn_id or self._response_user_turn_id
            self._pending_delegations[delegation_id] = owner
            while len(self._pending_delegations) > _MAX_PENDING_DELEGATIONS:
                del self._pending_delegations[next(iter(self._pending_delegations))]
            # Never forward straight away: the transcript of the sentence that
            # triggered this may still be arriving. The watchdog forwards once
            # the input has been quiet for _DELEGATION_SETTLE_S, or at the hard
            # deadline whatever has been heard so far.
            self._deferred_delegation = (delegation_id, owner, time.monotonic() + self._delegation_wait_s)
            have = len(self._user_transcript.strip())
        logger.info("[realtime] Delegation %s — settling the transcript (%d chars so far, up to %.0fms)",
                    delegation_id, have, self._delegation_wait_s * 1000)

    def _flush_deferred_delegation(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        with self._state_lock:
            deferred = self._deferred_delegation
            if deferred is None:
                return False
            did, owner, deadline = deferred
            # Only the transcript of the SAME input turn is the task text.
            message = self._user_transcript.strip() if self._live_user_turn_id == owner or not owner else ""
            settled = (
                bool(message)
                and self._last_input_at is not None
                and now - self._last_input_at >= _DELEGATION_SETTLE_S
            )
            if not force and not settled and now < deadline:
                return False
            self._deferred_delegation = None
        self._emit_delegation(did, owner, message)
        return True

    def _emit_delegation(self, delegation_id: str, owner: str, message: str) -> None:
        logger.info("[realtime] Delegation %s → %s(message=%r)", delegation_id, DELEGATE_TOOL_NAME, message[:100])
        with self._state_lock:
            gen = self._turn_gen
        self._recv_queue.put(OutputEvent(
            gen=gen,
            output=FunctionCallOutput(
                user_turn_id=_public_key(owner),
                name=DELEGATE_TOOL_NAME,
                arguments=json.dumps({"message": message}),
                call_id=delegation_id,
                user_transcript=message,
            ),
        ))

    def _on_backend_event(self, delegation_id: str | None, nested: Any) -> None:
        """A Responses API event from the backend, unwrapped from `response.event`."""
        if not isinstance(nested, dict):
            return
        ntype = str(nested.get("type") or "")
        if ntype == "response.output_item.done":
            item = nested.get("item") or {}
            itype = item.get("type")
            if itype == "function_call":
                call_id = str(item.get("call_id") or "")
                name = str(item.get("name") or "")
                args = item.get("arguments") or "{}"
                with self._state_lock:
                    owner = self._live_user_turn_id or self._response_user_turn_id
                    self._pending_function_calls[call_id] = delegation_id or ""
                    user_transcript = self._user_transcript.strip()
                logger.info("[realtime] Backend called %s (call_id=%s, delegation=%s)", name, call_id, delegation_id)
                self._recv_queue.put(OutputEvent(
                    gen=self._turn_gen,
                    output=FunctionCallOutput(
                        user_turn_id=_public_key(owner),
                        name=name,
                        arguments=args if isinstance(args, str) else json.dumps(args),
                        call_id=call_id,
                        user_transcript=user_transcript,
                    ),
                ))
            elif itype == "web_search_call":
                action = item.get("action") or {}
                logger.info("[realtime][web_search] backend searched: %r (status=%s)",
                            action.get("query"), item.get("status"))
        elif ntype in ("response.completed", "response.done", "response.incomplete", "response.failed"):
            with self._state_lock:
                self._backend_busy.pop(delegation_id or "", None)
            resp = nested.get("response") or {}
            usage = resp.get("usage") or {}
            usage_logger.info(
                "[realtime] GPT-Live backend usage: delegation=%s model=%s status=%s in=%s out=%s total=%s "
                "(billed as backend tokens on top of the voice minutes)",
                delegation_id or "-", resp.get("model") or self._config.backend_model, resp.get("status"),
                usage.get("input_tokens"), usage.get("output_tokens"), usage.get("total_tokens"),
            )
            if ntype == "response.failed":
                logger.warning("[realtime] GPT-Live backend response failed: %s", resp.get("error"))
        elif ntype == "error":
            logger.warning("[realtime] GPT-Live backend error: %s", nested.get("error") or nested)

    # --- Receive ---

    def _pump_events(self, conn: LiveConnection) -> bool:
        """Read events until the connection closes. Returns False on a clean
        close (the caller fail-fasts any reply in flight and reconnects); raises
        GPTLiveError on a fatal server error. Turn boundaries are NOT produced
        here — the watchdog does that from the output timestamps."""
        for event in conn:
            etype: str = getattr(event, "type", "")
            match etype:
                case "session.started":
                    session = getattr(event, "session", None)
                    self._session_id = getattr(session, "id", "") or ""
                    self._session_started_once = True
                    self._session_started.set()
                    logger.info(
                        "[realtime] GPT-Live session open (id=%s, expires_at=%s, voice=%s)",
                        self._session_id, getattr(session, "expires_at", None), self._config.voice,
                    )
                    self.note_server_activity()

                case "session.input_transcript.delta":
                    self.note_server_activity()
                    self._on_input_transcript(
                        getattr(event, "delta", "") or "",
                        getattr(event, "start_ms", None),
                        getattr(event, "end_ms", None),
                    )

                case "session.output_audio.delta":
                    # Liveness is noted inside: a silent delta is a heartbeat of
                    # the stream, not the model working on a reply.
                    self._on_output(audio=getattr(event, "delta", "") or "")

                case "session.output_transcript.delta":
                    self._on_output(text=getattr(event, "delta", "") or "")

                case "session.delegation.created":
                    self.note_server_activity()
                    d = getattr(event, "delegation", None)
                    self._on_delegation(getattr(d, "id", "") or "", getattr(d, "target", "") or "")

                case "response.event":
                    # The backend is working: a turn that is waiting on a web
                    # search is not a silent one.
                    self.note_server_activity()
                    self._on_backend_event(getattr(event, "delegation_id", None), getattr(event, "event", None))

                case "session.usage.updated":
                    # Liveness is NOT noted here: usage ticks say nothing about
                    # whether the model is working on a reply.
                    cw = getattr(event, "context_window", None)
                    ratio = getattr(cw, "usage_ratio", None)
                    if ratio is None:
                        ratio = getattr(event, "usage_ratio", None)
                    self._log_usage(getattr(event, "usage", None), usage_ratio=ratio)

                case "session.closed":
                    self._session_started.clear()
                    self._log_usage(getattr(event, "usage", None), closed=True)
                    logger.info("[realtime] GPT-Live session closed (reason=%s)", getattr(event, "reason", None))
                    self._session_closed.set()

                case "error":
                    self._on_error(event)

                case "info":
                    logger.info("[realtime] GPT-Live info %s: %s", getattr(event, "code", ""), getattr(event, "message", ""))

                case _:
                    # session.updated, session.*.appended, input_audio.muted /
                    # unmuted, response.event (Responses delegation only) …
                    pass
        return False

    def _on_error(self, event: Any) -> None:
        err = getattr(event, "error", None)
        client_id = getattr(event, "client_event_id", None) or getattr(err, "client_event_id", None)
        code = getattr(err, "code", None) or ""
        message = getattr(err, "message", None) or str(err)
        if client_id and client_id != _EVT_START and str(client_id).startswith("hal-"):
            # One of OUR commands was rejected (e.g. context over 500 tokens,
            # audio before session.started). The session is still fine.
            logger.warning("[realtime] GPT-Live rejected %s (%s): %s", client_id, code, message)
            return
        logger.error("[realtime] GPT-Live error (%s): %s", code, message)
        raise GPTLiveError(f"GPT-Live error ({code}): {message}")

    def _log_usage(self, usage: Any, *, closed: bool = False, usage_ratio: float | None = None) -> None:
        seconds = getattr(usage, "seconds", None)
        if seconds is None:
            return
        self._usage_seconds = float(seconds)
        if usage_ratio is not None:
            # Above ~0.9 OpenAI swaps in a fresh voice engine with a summary of
            # the history; authoritative task state must live on the device.
            self._usage_ratio = float(usage_ratio)
        usage_logger.info(
            "[realtime] GPT-Live usage: session=%s request=%s seconds=%.1f est>=$%.4f context=%s "
            "(%s; $%.2f/min billed per second, delegated main-agent work billed separately)",
            self._session_id or "-", self._request_id or "-", self._usage_seconds,
            self._usage_seconds / 60.0 * _GPTLIVE_USD_PER_MINUTE,
            "%.0f%%" % (self._usage_ratio * 100) if self._usage_ratio is not None else "-",
            "final" if closed else "cumulative", _GPTLIVE_USD_PER_MINUTE,
        )

    # --- Watchdog ---

    def _watchdog_loop(self) -> None:
        """Fires the synthesized turn boundary and flushes a deferred
        delegation. 50 ms tick: the boundary lands within that of the gap."""
        while not self._watchdog_stop.wait(0.05):
            now = time.monotonic()
            with self._state_lock:
                deadline = self._boundary_deadline
            if deadline is not None and now >= deadline:
                try:
                    self._fire_boundary()
                except Exception:  # never let the watchdog die
                    logger.exception("[realtime] GPT-Live boundary failed")
            try:
                self._flush_deferred_delegation()
            except Exception:
                logger.exception("[realtime] GPT-Live deferred delegation failed")

    def _start_watchdog(self) -> None:
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            return
        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="gptlive-watchdog",
        )
        self._watchdog_thread.start()

    def _stop_watchdog(self) -> None:
        self._watchdog_stop.set()
        thread = self._watchdog_thread
        self._watchdog_thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._config.join_timeout_s)

    # --- Reconnect ---

    def _ensure_connected(self) -> None:
        if self._stop_event.is_set() or self._connected.is_set():
            return
        now = time.monotonic()
        if now - self._last_reconnect_at < self._reconnect_backoff:
            return
        self._last_reconnect_at = now
        self._reconnect()

    def _reconnect(self) -> None:
        if self._stop_event.is_set():
            return
        with self._conn_lock:
            if self._stop_event.is_set() or self._connected.is_set():
                return
            try:
                logger.info("[realtime] Reconnecting...")
                self._sync_disconnect()
                self._sync_connect()
                self._connected.set()
                self._reconnect_backoff = self._reconnect_delay_s
            except Exception as e:
                self._reconnect_backoff = min(self._reconnect_backoff * 2, self._reconnect_backoff_max)
                logger.warning("[realtime] Reconnect failed: %s — next retry in ~%.0fs", e, self._reconnect_backoff)

    def _fail_fast_turn(self, reason: str) -> None:
        """End whatever the consumer is waiting on, now, so the turn falls back
        to the main agent without waiting out the receive timeout."""
        if self._fire_boundary(reason=reason):
            logger.info("[realtime] Recv error (%s) — reply ended early, falling back to main", reason)
            return
        with self._state_lock:
            waiting = self._awaiting_reply or bool(self._live_user_turn_id)
            self._awaiting_reply = False
            # The session that heard this input is gone; its answer will never
            # come, so the input key must not stay open into the next session.
            self._live_user_turn_id = ""
            self._live_speech_emitted = False
            self._input_answered = False
        if waiting:
            self._recv_queue.put(TurnDoneEvent())
            logger.info("[realtime] Recv error (%s) — ending turn now, falling back to main", reason)

    def _drop_connection(self, conn: LiveConnection | None) -> None:
        with self._conn_lock:
            if conn is None or self._connection is conn:
                self._connected.clear()
                self._session_started.clear()
                self._connection = None

    # --- VoiceAgentBase implementation ---

    @override
    def _do_connect(self) -> None:
        self._sync_connect()
        self._start_watchdog()

    @override
    def _do_disconnect(self) -> None:
        self._stop_watchdog()
        # Holding _conn_lock across the bounded wait is safe: the recv thread
        # reads `session.closed` without taking it, and a send racing this
        # teardown simply blocks until the connection is gone.
        with self._conn_lock:
            self._sync_disconnect(wait_closed=True)

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
                conn = self._connection
                try:
                    if isinstance(event, AudioCommitEvent):
                        self._sync_commit()
                    elif isinstance(event, InputEvent) and event.input is not None:
                        self._sync_send_input(event.input)
                    break
                except Exception as e:
                    if self._stop_event.is_set():
                        break
                    logger.exception("[realtime] Send failed (attempt %d/%d): %s", attempt + 1, self._max_retries, e)
                    self._drop_connection(conn)

    @override
    def _recv_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._connected.is_set():
                # Self-heal while disconnected even with no audio flowing.
                self._ensure_connected()
                if not self._connected.is_set():
                    self._connected.wait(timeout=self._queue_poll_s)
                continue
            for attempt in range(self._max_retries):
                self._ensure_connected()
                if self._stop_event.is_set():
                    break
                with self._conn_lock:
                    conn = self._connection if self._connected.is_set() else None
                if conn is None:
                    logger.debug("[realtime] Not connected, skipping attempt %d/%d", attempt + 1, self._max_retries)
                    continue
                try:
                    self._pump_events(conn)
                    if self._stop_event.is_set():
                        break
                    # Iteration ended: the session closed (expired, hangup, lost).
                    self._fail_fast_turn("session closed")
                    self._drop_connection(conn)
                    break
                except GPTLiveError as e:
                    if self._stop_event.is_set():
                        break
                    logger.warning("[realtime] Recv failed (attempt %d/%d): %s", attempt + 1, self._max_retries, e)
                    self._fail_fast_turn("api error")
                    self._drop_connection(conn)
                except ConnectionClosed as e:
                    if self._stop_event.is_set():
                        break
                    code = getattr(getattr(e, "rcvd", None), "code", None)
                    reason = getattr(getattr(e, "rcvd", None), "reason", "") or ""
                    self._note_close_code(code, reason)
                    self._fail_fast_turn(f"ws close {code}")
                    self._drop_connection(conn)
                except Exception as e:
                    if self._stop_event.is_set():
                        break
                    logger.exception("[realtime] Unexpected recv error (attempt %d/%d): %s", attempt + 1, self._max_retries, e)
                    self._fail_fast_turn("unexpected")
                    self._drop_connection(conn)

    def _note_close_code(self, code: int | None, reason: str) -> None:
        """Log a relay close the way the BFF integration doc reads it, and stop
        hammering the relay for the codes that will not change on their own."""
        meaning = _NO_RETRY_CLOSE_CODES.get(code or 0)
        if meaning is not None:
            self._reconnect_backoff = self._reconnect_backoff_max
            logger.warning(
                "[realtime] GPT-Live relay closed the session (%s): %s%s — next retry in ~%.0fs",
                code, meaning, f" [{reason}]" if reason else "", self._reconnect_backoff,
            )
        elif code == 1011:
            logger.warning("[realtime] GPT-Live relay could not reach OpenAI (1011%s) — retrying with backoff",
                           f": {reason}" if reason else "")
        elif code == 1000:
            logger.info("[realtime] GPT-Live session closed normally (1000%s)", f": {reason}" if reason else "")
        else:
            logger.warning("[realtime] GPT-Live socket closed (%s%s)", code, f": {reason}" if reason else "")
