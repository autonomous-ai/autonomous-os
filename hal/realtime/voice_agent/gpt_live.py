"""GPT-Live (OpenAI `/v1/live`, `gpt-live-1`) voice agent — queue-based threading, sync.

GPT-Live is NOT the Realtime API. It is a full-duplex model behind a different
WebSocket (`client.live.connect()`, `session.start` → `session.started`) whose
wire has none of the things the VoiceAgentBase contract was built around, so
this adapter SYNTHESIZES them and says so in each place:

  - No turn boundary (no `response.done` / `turn_complete`). A reply is over when
    no output audio or transcript has arrived for `turn_gap_ms`; a watchdog
    thread fires the TurnDoneEvent. See `_fire_boundary`.
  - No VAD events, no interruption event. User speech is observed ONLY through
    `session.input_transcript.delta` fragments (so `UserSpeechOutput` never
    carries a VAD `endpoint_at`). A fragment arriving while the model is
    speaking marks a barge-in candidate; if the model then falls silent for
    `interrupt_gap_ms` the reply counts as interrupted (queue drained,
    `InterruptedOutput(server_interrupt)`), if it keeps talking it was a
    backchannel. See `_on_input_transcript` / `_on_output`.
  - No tools at the Live layer. The session runs CLIENT delegation: when the
    model decides the request needs the backend it emits
    `session.delegation.created` (metadata only), which this adapter turns into
    the `delegate_to_main` FunctionCallOutput the orchestrator already handles.
    The task text is the accumulated input transcript. `express_emotion`,
    `reject_turn`, `end_conversation` and `look` cannot exist on this provider
    and are ignored with one log line each.
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

from hal import config as app_config
from hal.realtime.config import GPTLiveConfig
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
        self._session_id: str = ""
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
        self._first_audio_received: bool = False
        self._overlap_pending: bool = False
        self._overlap_at: float = 0.0
        self._boundary_deadline: float | None = None
        self._awaiting_reply: bool = False
        self._pending_delegations: dict[str, str] = {}
        self._deferred_delegation: tuple[str, str, float] | None = None
        self._usage_seconds: float = 0.0
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
            # Client delegation: the model emits session.delegation.created and
            # THIS process (→ the main agent) does the work. `responses` would
            # hand the work to an OpenAI-hosted model instead of our brain.
            "delegation": {"type": "client"},
        }

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
            self._first_audio_received = False
            self._overlap_pending = False
            self._boundary_deadline = None
            self._awaiting_reply = False
            self._pending_delegations = {}
            self._deferred_delegation = None

    def _sync_connect(self) -> None:
        # A reopened session must not inherit input ownership or delegations.
        self._reset_turn_state()
        self._session_started.clear()
        self._session_id = ""
        logger.info(
            "Connecting to GPT-Live (base_url=%s, model=%s)",
            self._config.base_url or "(SDK default)",
            self._config.model,
        )
        # No on_reconnecting → the SDK never reconnects on its own; the
        # send/recv loops below own recovery (backoff, fail-fast) like the
        # other providers.
        conn: LiveConnection = self._client.live.connect().enter()
        try:
            conn.session.start(session=self._build_session(), event_id=_EVT_START)
        except Exception:
            conn.close()
            raise
        self._connection = conn
        logger.info("[realtime] GPT-Live session.start sent (voice=%s)", self._config.voice)

    def _sync_disconnect(self) -> None:
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
        if text.startswith("[TTS HISTORY"):
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
                    self._input_answered = True
                    finished_only = True  # the input is complete: the model answers it
                else:
                    self._response_user_turn_id = ""
            if self._overlap_pending and now - self._overlap_at > self._interrupt_gap_s:
                # The model kept talking well past the user's words: backchannel.
                self._overlap_pending = False
            # Silence is measured from the LAST output either way. While the
            # overlap is still pending the window is the short one — the model
            # stopping within it is the interruption — but continuous output
            # keeps pushing the deadline, so a model that talks straight through
            # the user's words is never cut off by us.
            self._boundary_deadline = now + (
                self._interrupt_gap_s if self._overlap_pending else self._turn_gap_s
            )
            owner = _public_key(self._response_user_turn_id)
            gen = self._turn_gen
            first_audio = audio is not None and not self._first_audio_received
            if first_audio:
                self._first_audio_received = True
            last_input_at = self._last_input_at
            chunk_index = self._output_transcript_chunks
            if text is not None:
                self._output_transcript_chunks += 1
        if finished_only:
            self._observe_user_speech(transcript_finished=True)
        if audio is not None:
            if first_audio and last_input_at is not None:
                logger.info(
                    "[realtime] Response latency: %.0fms (last_input_transcript->first_audio; full duplex)",
                    (now - last_input_at) * 1000,
                )
            self._recv_queue.put(OutputEvent(
                gen=gen, output=AudioOutput(user_turn_id=owner, audio=base64_pcm16_to_float32(audio)),
            ))
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
            self._output_active = False
            self._overlap_pending = False
            self._boundary_deadline = None
            self._first_audio_received = False
            self._output_transcript_chunks = 0
            if owner and self._live_user_turn_id == owner:
                # This input has been answered; a later unsolicited remark must
                # not be attributed to it.
                self._live_user_turn_id = ""
                self._live_speech_emitted = False
        if interrupted:
            self._drain_for_interrupt(_public_key(owner))
        self._recv_queue.put(TurnDoneEvent(
            execution_completed=(reason == "gap" and not interrupted),
            user_turn_id=_public_key(owner),
        ))
        logger.debug("[realtime] GPT-Live turn boundary (%s%s) owner=%s",
                     reason, ", interrupted" if interrupted else "", owner or "-")
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
            logger.info("[realtime] Ignoring %s delegation %s (not client-owned)", target, delegation_id)
            return
        with self._state_lock:
            owner = self._live_user_turn_id or self._response_user_turn_id
            self._pending_delegations[delegation_id] = owner
            while len(self._pending_delegations) > _MAX_PENDING_DELEGATIONS:
                del self._pending_delegations[next(iter(self._pending_delegations))]
            message = self._user_transcript.strip()
            if not message:
                self._deferred_delegation = (delegation_id, owner, time.monotonic() + self._delegation_wait_s)
                logger.info("[realtime] Delegation %s before any transcript — waiting up to %.0fms",
                            delegation_id, self._delegation_wait_s * 1000)
                return
        self._emit_delegation(delegation_id, owner, message)

    def _flush_deferred_delegation(self, *, force: bool = False) -> bool:
        with self._state_lock:
            deferred = self._deferred_delegation
            if deferred is None:
                return False
            did, owner, deadline = deferred
            if not force and time.monotonic() < deadline:
                return False
            self._deferred_delegation = None
            # Only the transcript of the SAME input turn is the task text.
            message = self._user_transcript.strip() if self._live_user_turn_id == owner or not owner else ""
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
                    self.note_server_activity()
                    self._on_output(audio=getattr(event, "delta", "") or "")

                case "session.output_transcript.delta":
                    self.note_server_activity()
                    self._on_output(text=getattr(event, "delta", "") or "")

                case "session.delegation.created":
                    self.note_server_activity()
                    d = getattr(event, "delegation", None)
                    self._on_delegation(getattr(d, "id", "") or "", getattr(d, "target", "") or "")

                case "session.usage.updated":
                    # Liveness is NOT noted here: usage ticks say nothing about
                    # whether the model is working on a reply.
                    self._log_usage(getattr(event, "usage", None))

                case "session.closed":
                    self._session_started.clear()
                    self._log_usage(getattr(event, "usage", None), closed=True)
                    logger.info("[realtime] GPT-Live session closed (reason=%s)", getattr(event, "reason", None))

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

    def _log_usage(self, usage: Any, *, closed: bool = False) -> None:
        seconds = getattr(usage, "seconds", None)
        if seconds is None:
            return
        self._usage_seconds = float(seconds)
        usage_logger.info(
            "[realtime] GPT-Live usage: session=%s seconds=%.1f est>=$%.4f "
            "(%s; $%.2f/min billed per second, delegated main-agent work billed separately)",
            self._session_id or "-", self._usage_seconds,
            self._usage_seconds / 60.0 * _GPTLIVE_USD_PER_MINUTE,
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
        with self._conn_lock:
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
                except Exception as e:
                    if self._stop_event.is_set():
                        break
                    logger.exception("[realtime] Unexpected recv error (attempt %d/%d): %s", attempt + 1, self._max_retries, e)
                    self._fail_fast_turn("unexpected")
                    self._drop_connection(conn)
