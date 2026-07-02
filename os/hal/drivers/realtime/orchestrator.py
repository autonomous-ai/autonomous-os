"""Realtime orchestrator — manages voice agent lifecycle and turn processing.

Exposes a simple interface to the voice pipeline:
  - append_audio(frame) — queues audio to the model (non-blocking)
  - commit_audio() — queues commit signal (non-blocking)
  - stream_output() — yields outputs one by one as they arrive

The caller (voice_service) drives the orchestrator:
  1. Stream mic frames via append_audio()
  2. Call commit_audio() when done
  3. Iterate stream_output():
     - Yields AudioOutput / TextOutput / FunctionCallOutput chunks
     - Yields DelegateSignal if model called delegate_to_main (then stops)
"""

import json
import logging
import threading
import time
from collections.abc import Generator
from typing import Any

import numpy as np
import numpy.typing as npt

import hal.config as config
import hal.presets as presets
from hal.drivers.realtime.config import (
    GeminiConfig,
    OpenAIConfig,
    _load_language,
    gemini_needs_idle_workaround,
)
from hal.drivers.realtime.context_manager import (
    ContextManagerBase,
    HermesContextManager,
    OpenClawContextManager,
)
from hal.drivers.realtime.enums import AgentGateway
from hal.drivers.realtime.models import (
    FunctionCallOutput,
    FunctionCallResultInput,
    ImageInput,
    OutputBase,
    TextInput,
)
from hal.drivers.realtime.models.signal import DelegateSignal, LookReplaySignal
from hal.drivers.realtime.summarizer import RealtimeSummarizer
from hal.drivers.realtime.voice_agent.base import VoiceAgentBase

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_RATE: int = 16000

DELEGATE_TOOL_NAME: str = "delegate_to_main"
DELEGATE_TOOL_DESCRIPTION: str = (
    "Call this when the user's request requires the main system — "
    "device control, music, scheduling, memory, skills, real-time facts, "
    "or anything beyond casual conversation. "
    "Pass a message summarizing what the user wants so the main system "
    "can act without re-listening to the audio. "
    "ONLY call this when you clearly understood an actual request. NEVER "
    "invent, guess, or infer a request from unclear, minimal, or noise-like "
    "audio (e.g. 'oh', 'uh', a cough, a single unclear syllable) — if you are "
    "not sure what the user wants, do NOT delegate; stay silent instead. "
    "The message must reflect what the user actually said, never a fabrication."
)

DELEGATE_TOOL: dict[str, Any] = {
    "type": "function",
    "name": DELEGATE_TOOL_NAME,
    "description": DELEGATE_TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "A short summary of what the user ACTUALLY asked for, to pass to the main system. Must not be empty and must not be invented — if you didn't clearly understand a request, don't call this tool at all.",
            },
        },
        "required": ["message"],
    },
}

DEFAULT_EMOTION_INTENSITY: float = 0.8

EMOTION_TOOL_NAME: str = "express_emotion"
# Conversational emotions the agent may set to match its own spoken tone.
# Excludes system/background states (idle, listening, sleepy, scan, nod, music_*)
# which are driven by the device, not the agent.
EMOTION_TOOL_EMOTIONS: list[str] = [
    presets.EMO_HAPPY, presets.EMO_EXCITED, presets.EMO_CURIOUS,
    presets.EMO_THINKING, presets.EMO_CARING, presets.EMO_LAUGH,
    presets.EMO_SHY, presets.EMO_SAD, presets.EMO_SHOCK,
    presets.EMO_CONFUSED, presets.EMO_GREETING, presets.EMO_GOODBYE,
]
EMOTION_TOOL_DESCRIPTION: str = (
    "Set the device's physical face (LED + servo) to match the emotional tone "
    "of the reply you are ABOUT TO SPEAK. This is FIRE-AND-FORGET and is the ONE "
    "exception to the binary tool rule: it does NOT delegate and does NOT replace "
    "speech — call it IN PARALLEL with speaking, then immediately speak your reply. "
    "Never wait for it, never mention it, never speak the emotion name or any "
    "marker syntax aloud. Calling it is optional; only call it when an emotion "
    "clearly fits your reply. Available emotions: " + ", ".join(EMOTION_TOOL_EMOTIONS) + "."
)

EMOTION_TOOL: dict[str, Any] = {
    "type": "function",
    "name": EMOTION_TOOL_NAME,
    "description": EMOTION_TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "emotion": {
                "type": "string",
                "enum": EMOTION_TOOL_EMOTIONS,
                "description": "The facial emotion that matches the tone of the reply you are about to speak.",
            },
            "intensity": {
                "type": "number",
                "description": "Expression strength from 0.0 (subtle) to 1.0 (full). Defaults to about 0.8.",
            },
        },
        "required": ["emotion"],
    },
}

LOOK_TOOL_NAME: str = "look"
LOOK_TOOL_DESCRIPTION: str = (
    "Capture a single frame from the device's camera and look at it, so you can "
    "answer a question about what you SEE right now (e.g. 'what is this?', 'what "
    "am I holding?', 'what's in front of you?', 'read this label', 'what color is "
    "this?'). Unlike delegate_to_main, this does NOT hand off — call it, the image "
    "is added to your context, then you immediately SPEAK your answer in this same "
    "turn. The scene CHANGES constantly: the user may have swapped objects since "
    "the last image, so for ANY present-tense visual question you MUST call this "
    "again — never answer from a previous image, from memory, or from the "
    "conversation, even if you are sure you already know; that is exactly how you "
    "get it embarrassingly wrong. Do NOT use it for non-visual requests."
)

# No parameters: the model just signals intent to look; the device grabs the
# current frame. Kept empty so the call is as cheap/fast as possible.
LOOK_TOOL: dict[str, Any] = {
    "type": "function",
    "name": LOOK_TOOL_NAME,
    "description": LOOK_TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {},
    },
}


def _camera_present() -> bool:
    """True when a camera is wired up — the device's `vision` capability at
    runtime. app_state.camera_capture is only set by server.py when DEVICE.md
    declares `vision`, so this is the single source of truth for "can look".
    Lazy import keeps app_state out of this module's import graph."""
    try:
        import hal.app_state as state

        return state.camera_capture is not None
    except Exception:
        return False


class RealtimeOrchestrator:
    """Manages a single realtime voice agent session.

    Automatically registers the delegate_to_main tool so the model
    can signal that the user's request should be handled by the main
    flow (device → OpenClaw).
    """

    CONTEXT_MANAGERS: dict[str, type[ContextManagerBase]] = {
        AgentGateway.OPENCLAW: OpenClawContextManager,
        AgentGateway.HERMES: HermesContextManager,
    }

    WORKSPACE_DIRS: dict[str, str] = {
        AgentGateway.OPENCLAW: config.OPENCLAW_WORKSPACE_DIR,
        AgentGateway.HERMES: config.HERMES_WORKSPACE_DIR,
    }

    def __init__(
        self,
        gateway: AgentGateway = AgentGateway.OPENCLAW,
        extra_tools: list[dict[str, Any]] | None = None,
        enable_expression: bool = False,
    ) -> None:
        # express_emotion is registered ONLY when the device declares the
        # `expression` capability (DEVICE.md → expression: { routes: [emotion] }).
        # A device with no face (e.g. mic+speaker only) never sees the tool, so
        # the model can't call it and nothing fires.
        self._expression_enabled: bool = enable_expression
        tools: list[dict[str, Any]] = [DELEGATE_TOOL]
        if enable_expression:
            tools.append(EMOTION_TOOL)
        # `look` (in-session vision) is registered only when ALL hold: a camera is
        # present, the config flag is on (REALTIME_GEMINI_VISION), and the provider
        # is Gemini (the image-injection → continue-turn flow is implemented +
        # tested for Gemini Live; OpenAI keeps delegating). Otherwise visual
        # questions fall back to delegate_to_main.
        #
        # Camera presence IS the device's `vision` capability at runtime: server.py
        # only creates app_state.camera_capture when DEVICE.md declares `vision`
        # (the `camera` route mounts). Reusing that one signal — the same one
        # /health and _capture_frame read — means no extra capability plumbing and
        # it's correct for EVERY construction path (auto-start and /voice/start).
        self._vision_enabled: bool = (
            _camera_present()
            and config.REALTIME_GEMINI_VISION
            and config.REALTIME_PROVIDER.strip().lower() == "gemini"
        )
        if self._vision_enabled:
            tools.append(LOOK_TOOL)
        self._tools: list[dict[str, Any]] = tools + (extra_tools or [])
        # `look` cost guards: at most ONE image sent per turn, and no fresh image
        # within VISION_MIN_INTERVAL_S of the last send (the recent frame is still
        # in context, so repeat looks reuse it instead of re-billing image tokens).
        self._looked_this_turn: bool = False
        self._last_look_sent_monotonic: float = 0.0
        self._agent: VoiceAgentBase | None = None
        self._started: threading.Event = threading.Event()
        self._consecutive_silent: int = 0  # zombie-session guard (see stream_output)
        # Idle session recycle (cost): when a turn arrives after a long silence,
        # rebuild the session AFTER that turn so the next turn drops the context
        # the provider re-bills on a long-lived session. See _maybe_idle_reset.
        self._last_turn_monotonic: float = 0.0  # end-of-turn timestamp; 0 = none yet
        self._idle_reset_pending: bool = False
        self._turns_since_recycle: int = 0  # turn-cap recycle counter (cost)
        self._rebuild_lock: threading.Lock = threading.Lock()  # guards _force_rebuild
        summarizer: RealtimeSummarizer | None = None
        if config.REALTIME_SUMMARIZER_ENABLED:
            try:
                summarizer = RealtimeSummarizer()
                logger.info(
                    "Realtime summarizer enabled (model=%s)",
                    config.REALTIME_SUMMARIZER_MODEL,
                )
            except Exception as e:
                logger.warning("Failed to create summarizer: %s", e)
        context_cls = self.CONTEXT_MANAGERS.get(gateway, OpenClawContextManager)
        self._context: ContextManagerBase = context_cls(
            workspace_dir=self.WORKSPACE_DIRS.get(
                gateway, config.OPENCLAW_WORKSPACE_DIR
            ),
            language=_load_language() or "English",
            provider=config.REALTIME_PROVIDER,
            summarizer=summarizer,
        )

    @property
    def available(self) -> bool:
        # Require the agent to be actually CONNECTED, not just constructed. On a
        # Gemini usage-limit close (code 4029) or any failed reconnect the agent
        # object survives but its session is dead; without the _agent.available
        # check, voice_service still routes the turn to realtime, commits, and
        # blocks ~15s in receive() before falling back. Gating on the live
        # connection makes those turns skip realtime and go straight to the main
        # agent immediately (and auto-resume once the limit clears / reconnect wins).
        #
        # Also report unavailable WHILE a session rebuild is in flight. _force_rebuild
        # keeps the old agent serving until the new one connects (~1s) then swaps and
        # disconnects the old — so a turn that dispatches during that window commits
        # its audio to the about-to-be-discarded old session and dies mid-turn with
        # WS 1000 (the "async rebuild raced the next turn" failure). Gating on the
        # rebuild lock routes such turns cleanly to the main agent for that ~1s
        # instead, eliminating that whole class of mid-turn closes.
        return (
            self._started.is_set()
            and self._agent is not None
            and self._agent.available
            and not self._rebuild_lock.locked()
        )

    @property
    def sample_rate(self) -> int:
        """Target sample rate expected by the realtime provider."""
        if self._agent is not None:
            return self._agent.sample_rate
        return DEFAULT_SAMPLE_RATE

    @property
    def output_sample_rate(self) -> int:
        """Sample rate of the model's own audio output (for native playback)."""
        if self._agent is not None:
            return self._agent.output_sample_rate
        return DEFAULT_SAMPLE_RATE

    def _make_agent(self, provider: str, instructions: str) -> VoiceAgentBase | None:
        """Build (not connect) a fresh agent for the provider. Shared by start()
        and the zombie rebuild so both create the agent identically."""
        if provider == "gemini":
            from hal.drivers.realtime.voice_agent.gemini_live import GeminiLiveAgent

            return GeminiLiveAgent(
                config=GeminiConfig(instructions=instructions), tools=self._tools,
            )
        if provider == "openai":
            from hal.drivers.realtime.voice_agent.openai_realtime import (
                OpenAIRealtimeAgent,
            )

            return OpenAIRealtimeAgent(
                config=OpenAIConfig(instructions=instructions), tools=self._tools,
            )
        return None

    def _rebuild_now(self, reason: str) -> bool:
        """Synchronously swap in a fresh session before audio is streamed.

        Used for Gemini idle-gap recovery: if the proxy/SDK may have dropped an
        idle socket, reconnecting after commit is too late because this turn's
        audio has already been sent to the dead session.
        """
        if not self._rebuild_lock.acquire(blocking=False):
            return False
        provider: str = config.REALTIME_PROVIDER.strip().lower()
        old = self._agent
        try:
            instructions = self._context.build_instructions()
            new = self._make_agent(provider, instructions)
            if new is None:
                return False
            new.connect()
            self._agent = new
            self._consecutive_silent = 0
            self._idle_reset_pending = False
            self._turns_since_recycle = 0
            # Images live in the session context — a fresh session has none, so
            # the look reuse-guard must not claim "recent frame still in context"
            # (it would ack the model with no image there → silent turn).
            self._looked_this_turn = False
            self._last_look_sent_monotonic = 0.0
            logger.info("[realtime] Fresh session connected before turn (%s)", reason)
            return True
        except Exception:
            logger.exception("[realtime] Pre-turn session rebuild failed")
            return False
        finally:
            self._rebuild_lock.release()
            if old is not None and old is not self._agent:
                try:
                    old.disconnect()
                except Exception:
                    logger.exception("[realtime] old agent disconnect failed")

    def recover_session(self, reason: str) -> bool:
        """Reconnect a FRESH session synchronously for a mid-turn 1011 replay.

        Used by run_realtime_turn when a turn produced no output (likely WS 1011
        from a proxy-dropped idle session): swap in a fresh agent so the caller
        can immediately replay the captured turn audio onto an active session.
        Returns True if a fresh session is ready.
        """
        return self._rebuild_now(reason)

    def prepare_turn(self) -> None:
        """Prepare the realtime session before the caller streams turn audio."""
        provider: str = config.REALTIME_PROVIDER.strip().lower()
        if provider != "gemini" or not gemini_needs_idle_workaround():
            return
        threshold = config.REALTIME_GEMINI_PRE_TURN_RECYCLE_S
        if threshold <= 0 or self._last_turn_monotonic <= 0.0:
            return
        idle = time.monotonic() - self._last_turn_monotonic
        if idle < threshold:
            return
        logger.info(
            "[realtime] %.0fs idle (>= %.0fs) — recycling Gemini before streaming audio",
            idle,
            threshold,
        )
        self._rebuild_now("gemini-idle-pre-turn")

    def _force_rebuild(self) -> None:
        """Recover a zombie session by building a BRAND-NEW agent and swapping it
        in, then discarding the old one.

        We do NOT reconnect the existing agent: its recv thread is wedged inside
        `async for session.receive()` on a dead socket, and closing that session
        does not reliably unblock it — so an in-place reconnect never fires (the
        send loop only reconnects on a queued input, but inputs are gated on
        `available`, which is False once we clear `_connected` → deadlock). A
        fresh agent has its own loop / threads / session, sidestepping the wedge
        entirely (this is what a HAL restart does, minus the process). Runs on a
        daemon thread so the voice turn doesn't block on connect; a lock prevents
        overlapping rebuilds while one is in flight."""
        if not self._rebuild_lock.acquire(blocking=False):
            return  # a rebuild is already running
        provider: str = config.REALTIME_PROVIDER.strip().lower()

        def _run() -> None:
            old = self._agent
            try:
                instructions = self._context.build_instructions()
                new = self._make_agent(provider, instructions)
                if new is None:
                    return
                new.connect()
                self._agent = new
                # Fresh session context has no images — see _rebuild_now.
                self._looked_this_turn = False
                self._last_look_sent_monotonic = 0.0
                logger.info("[realtime] Zombie recovery: fresh session connected")
            except Exception:
                logger.exception("[realtime] Zombie rebuild failed")
            finally:
                self._rebuild_lock.release()
                # Tear down the old (wedged) agent in the background — its recv
                # thread only exits once its loop is stopped by disconnect(); it
                # is a separate instance so it can't disturb the new agent.
                if old is not None and old is not self._agent:
                    try:
                        old.disconnect()
                    except Exception:
                        logger.exception("[realtime] old agent disconnect failed")

        threading.Thread(
            target=_run, daemon=True, name="rt-zombie-rebuild"
        ).start()

    def start(self) -> None:
        """Create the agent based on config and connect."""
        provider: str = config.REALTIME_PROVIDER.strip().lower()
        if provider in ("none", "off", "disabled", ""):
            logger.info("Realtime orchestrator disabled (provider=%s)", provider)
            return

        instructions: str = self._context.build_instructions()
        logger.info(
            "[realtime] Context manager built instructions (%d chars)",
            len(instructions),
        )

        self._agent = self._make_agent(provider, instructions)
        if self._agent is None:
            logger.warning("Unknown realtime provider: %s — disabled", provider)
            return

        try:
            self._agent.connect()
            logger.info(
                "[realtime] Realtime orchestrator started (provider=%s)", provider
            )
        except Exception:
            logger.exception(
                "[realtime] Failed to connect realtime agent — will retry on next audio"
            )
        self._started.set()

        # Catch up on any unsummarized memory from a previous (possibly crashed)
        # session in the background. This is an LLM call and is NOT needed to serve
        # turns — running it before connect would keep `available` False for seconds
        # after a restart, leaking early turns ("hello") to the main agent. The
        # summarizer is concurrency-safe (keeps entries appended during summarization),
        # so it is safe to run alongside the live session.
        threading.Thread(
            target=self._catch_up_memory_summaries,
            daemon=True,
            name="realtime-catchup-summarize",
        ).start()

    def _catch_up_memory_summaries(self) -> None:
        """Summarize memory left unsummarized by a previous session (background)."""
        try:
            self._context.summarize_device_memory()
            self._context.summarize_realtime_memory()
        except Exception:
            logger.exception("[realtime] Failed to catch up on memory summarization")

    def stop(self) -> None:
        """Disconnect the agent and summarize unsummarized memory."""
        # Summarize remaining memory before shutdown
        try:
            self._context.summarize_device_memory()
            self._context.summarize_realtime_memory()
        except Exception:
            logger.exception("[realtime] Failed to summarize memory on shutdown")

        if self._agent is not None:
            try:
                self._agent.disconnect()
            except Exception:
                logger.exception("Failed to disconnect realtime agent")
            self._agent = None
        self._started.clear()
        logger.info("Realtime orchestrator stopped")

    def append_audio(self, frame: npt.NDArray[np.float32]) -> None:
        """Queue a single audio frame to the model (non-blocking)."""
        if self._agent is not None:
            self._agent.append_audio(frame)

    def commit_audio(self) -> None:
        """Queue commit signal (non-blocking)."""
        self._mark_turn_start()
        if self._agent is not None:
            self._agent.commit_audio()

    def _mark_turn_start(self) -> None:
        """Detect a long idle gap before this turn and arm a post-turn session
        recycle. We measure end-of-previous-turn → start-of-this-turn (the silence
        gap), and only ARM here — the rebuild itself fires at the END of
        stream_output so it never swaps self._agent while this turn's audio
        (already appended to the current session) is mid-commit. Cost rationale:
        a session that has been chatting for a while re-bills its accumulated
        context every turn; a turn that follows a long pause is effectively a new
        conversation, so recycling then drops that context for ~free (long-term
        continuity is preserved via the summary the rebuild reloads)."""
        reset_s = config.REALTIME_SESSION_IDLE_RESET_S
        if reset_s <= 0 or self._last_turn_monotonic <= 0.0:
            return
        idle = time.monotonic() - self._last_turn_monotonic
        if idle >= reset_s:
            logger.info(
                "[realtime] %.0fs idle (>= %.0fs) — will recycle session after this "
                "turn to drop accumulated context (cost)",
                idle,
                reset_s,
            )
            self._idle_reset_pending = True

    def flush_output(self) -> None:
        """Discard buffered outputs from a prior turn before committing a new one.

        Prevents a stale response (from an earlier, possibly noise-triggered turn)
        being read and spoken on the current turn — see VoiceAgentBase.flush_output.
        """
        if self._agent is not None:
            self._agent.flush_output()

    def stream_output(
        self,
    ) -> Generator[OutputBase | DelegateSignal | LookReplaySignal, None, None]:
        """Yield outputs from the model one by one as they arrive.

        Yields:
          - AudioOutput / TextOutput / FunctionCallOutput as they stream in
          - DelegateSignal if model called delegate_to_main (then stops)
          - LookReplaySignal if model called look and a fresh frame was sent —
            the caller must re-append the turn's audio and commit again so the
            frame joins the replayed turn (then stops)

        The generator returns (StopIteration) when the model's turn is done.
        """
        if self._agent is None:
            return

        self._looked_this_turn = False  # reset the per-turn `look` image-send guard
        produced = False  # did this turn yield any real output (vs stay silent)?
        replay_pending = False  # look-replay signalled — the turn continues
        for output in self._agent.receive(stop_on_done=True):
            if (
                isinstance(output, FunctionCallOutput)
                and output.name == LOOK_TOOL_NAME
            ):
                # Fresh frame sent → the turn must be REPLAYED so the frame
                # (queued by the Live API for the next turn) joins the answer —
                # see _handle_look_call. Mirror the delegate flow: end_turn so
                # the replay's commit isn't gated on a turn_complete that never
                # comes, signal the turn driver, stop this turn.
                if self._handle_look_call(output):
                    produced = True
                    replay_pending = True
                    # Unblock the replay's commit (no turn_complete follows a
                    # tool-call-only turn) and arm receive() to swallow the
                    # cancelled turn's LATE turn_complete, which otherwise ends
                    # the replayed turn empty ~300ms in.
                    self._agent.end_turn()
                    self._agent.skip_next_turn_done()
                    yield LookReplaySignal()
                    break
                # Reused/absent frame → the tool was acked; the turn continues
                # and the spoken answer is yielded by the branches below.
                continue
            if (
                isinstance(output, FunctionCallOutput)
                and output.name == EMOTION_TOOL_NAME
            ):
                self._handle_emotion_call(output)
                continue
            if (
                isinstance(output, FunctionCallOutput)
                and output.name == DELEGATE_TOOL_NAME
            ):
                delegate_msg: str = ""
                try:
                    args: dict[str, Any] = (
                        json.loads(output.arguments) if output.arguments else {}
                    )
                    delegate_msg = args.get("message", "").strip()
                except (ValueError, TypeError):
                    pass

                if not delegate_msg:
                    logger.warning(
                        "[realtime] Model called delegate_to_main with empty message — ignoring"
                    )
                    self._agent.send(
                        [
                            FunctionCallResultInput(
                                call_id=output.call_id,
                                output='{"error": "message must not be empty"}',
                            )
                        ]
                    )
                    continue

                logger.info(
                    "[realtime] Model delegated to main flow (message=%r)",
                    delegate_msg[:100],
                )
                self._agent.send(
                    [
                        FunctionCallResultInput(
                            call_id=output.call_id,
                            output='{"result": "delegated"}',
                        )
                    ]
                )
                produced = True
                # Mark the turn done so the NEXT turn's commit isn't gated on a
                # turn_complete that never comes after a tool call (Gemini manual
                # VAD waits up to 10s on _turn_done otherwise).
                self._agent.end_turn()
                yield DelegateSignal(message=delegate_msg)
                # Stop the turn here — once the model has delegated, it has nothing
                # more to say, and waiting for turn_complete just blocks on the
                # receive() timeout (the model stays silent for the full
                # RECV_QUEUE_TIMEOUT, ~15s) before we forward to the main flow. We
                # already sent the function result above; the dangling open turn is
                # dropped by the next turn's flush_output(). Forward immediately.
                break
            produced = True
            yield output

        # Look-replay pending: the logical turn CONTINUES (the caller is about
        # to re-commit this turn's audio on the SAME session). Don't stamp,
        # count, or recycle here — an idle/turn-cap recycle at this point swaps
        # in a fresh session and orphans the image that was just sent to the
        # old one (device-observed 2026-07-02: replay ran on a blank session,
        # the model had no frame and stayed silent → main-agent fallback). Any
        # armed recycle stays pending and fires after the replayed turn.
        if replay_pending:
            return

        # Track consecutive silent turns for the zombie guard: a turn that
        # committed audio but yielded nothing. A long-lived Gemini session can
        # wedge (connected, accepts audio, never replies — no go_away/close), so
        # N consecutive silent turns means force a fresh session. Reset on any
        # real output so interspersed noise turns don't trip it.
        if produced:
            self._consecutive_silent = 0
        else:
            self._consecutive_silent += 1

        # Stamp end-of-turn (next turn's idle measurement) + count this turn.
        self._last_turn_monotonic = time.monotonic()
        self._turns_since_recycle += 1

        # Recycle the session (rebuild → drop the context the provider re-bills
        # every turn) for any of three reasons, decided in ONE place so the
        # rebuild never double-fires and all state resets together. Done between
        # turns so it never swaps self._agent mid-turn; rebuild is async so the
        # NEXT turn uses the fresh session. Continuity survives via summary.md.
        #   - zombie:   N consecutive silent turns (wedged session)
        #   - idle:     this turn followed a long silence (see _mark_turn_start)
        #   - turn-cap: session handled enough turns that context grew large (cost)
        # NOTE: NO grounding-triggered recycle. Recycling after every Google-Search
        # turn churned the session and the async rebuild raced the next turn (common
        # when the user asks consecutive search questions) → frequent mid-turn WS 1000
        # closes. The snippet-eviction saving (~200t) wasn't worth it; turn-cap/idle
        # recycle already bounds accumulation. The race itself (rebuild swapping the
        # agent under a dispatching turn) is now also blocked by the available-gate
        # checking _rebuild_lock — see the `available` property.
        zombie: bool = (
            not produced
            and self._consecutive_silent >= config.REALTIME_ZOMBIE_RECONNECT_AFTER
        )
        max_turns: int = config.REALTIME_SESSION_MAX_TURNS
        turn_cap: bool = max_turns > 0 and self._turns_since_recycle >= max_turns
        if zombie or self._idle_reset_pending or turn_cap:
            if zombie:
                logger.warning(
                    "[realtime] %d consecutive silent turns — forcing reconnect "
                    "(zombie session)",
                    self._consecutive_silent,
                )
            else:
                reason: str = "idle" if self._idle_reset_pending else "turn-cap"
                logger.info(
                    "[realtime] recycling session (%s) after %d turns (cost)",
                    reason, self._turns_since_recycle,
                )
            self._consecutive_silent = 0
            self._idle_reset_pending = False
            self._turns_since_recycle = 0
            self._force_rebuild()

    def _handle_emotion_call(self, output: FunctionCallOutput) -> None:
        """Fire the device's emotion expression without blocking the spoken turn.

        Fire-and-forget: the HAL /emotion call runs in a daemon thread (parallel
        to the audio already streaming), and the function result is acknowledged
        with trigger_response=False so it does NOT spawn a second model response.
        Net added latency to speech is ~zero.
        """
        emotion: str = ""
        intensity: float = DEFAULT_EMOTION_INTENSITY
        try:
            args: dict[str, Any] = (
                json.loads(output.arguments) if output.arguments else {}
            )
            emotion = str(args.get("emotion", "")).strip().lower()
            intensity = float(args.get("intensity", DEFAULT_EMOTION_INTENSITY))
        except (ValueError, TypeError):
            pass
        intensity = max(0.0, min(1.0, intensity))

        if emotion:
            threading.Thread(
                target=self._fire_emotion,
                args=(emotion, intensity),
                daemon=True,
            ).start()
            logger.info(
                "[realtime] express_emotion fired (emotion=%s intensity=%.2f)",
                emotion,
                intensity,
            )
        else:
            logger.warning(
                "[realtime] express_emotion called with empty emotion — ignoring"
            )

        # Acknowledge the call in history but do NOT trigger a new response.
        if self._agent is not None:
            self._agent.send(
                [
                    FunctionCallResultInput(
                        call_id=output.call_id,
                        output='{"result": "expressed"}',
                        trigger_response=False,
                    )
                ]
            )

    @staticmethod
    def _fire_emotion(emotion: str, intensity: float) -> None:
        """Drive the device face by calling the HAL emotion handler in-process.

        The realtime agent runs inside the HAL process, so we call the route
        handler directly instead of looping back over HTTP — no serialization,
        no network stack, lower latency. Runs in a daemon thread; logs the
        outcome (status ok / ignored) so device testing can confirm the emotion
        actually fired and measure how long it took.
        """
        started: float = time.monotonic()
        try:
            # Lazy import: the emotion handler pulls in app_state / LED / servo;
            # keep that out of the driver's module-load graph and avoid any cycle.
            from hal.models import EmotionRequest
            from hal.routes.emotion import express_emotion as hal_express_emotion

            result: Any = hal_express_emotion(
                EmotionRequest(emotion=emotion, intensity=intensity)
            )
            took_ms: float = (time.monotonic() - started) * 1000
            status: str = (
                result.get("status", "?") if isinstance(result, dict) else "?"
            )
            logger.info(
                "[realtime] emotion expressed (emotion=%s intensity=%.2f status=%s %.0fms)",
                emotion,
                intensity,
                status,
                took_ms,
            )
        except Exception as e:
            logger.warning(
                "[realtime] emotion expression failed (emotion=%s): %s", emotion, e
            )

    def _handle_look_call(self, output: FunctionCallOutput) -> bool:
        """Handle the model's `look` call. Returns True when a FRESH frame was
        sent and the turn must be REPLAYED (see LookReplaySignal), False when
        the turn should just continue (frame reused from context, or no camera).

        Why replay: the Live API queues a frame sent MID-TURN for the NEXT
        turn — the tool-ack → continue-turn flow made the model answer every
        look from the PREVIOUS look's image (device-proven 2026-07-02; neither
        ack delays nor client_content injection fix it). So on a fresh frame we
        send the image, do NOT ack the tool call (the replayed audio activity
        cancels the pending turn), and let the turn driver re-commit the user's
        audio — the new turn picks up the queued frame and the model answers
        about the CURRENT scene.

        Reuse path (already looked this turn / within VISION_MIN_INTERVAL_S of
        the last send): the recent frame is genuinely in context — in the
        replay turn this is exactly how the model reads the fresh frame — so
        ack with trigger_response=True and let the turn continue.
        """
        if self._agent is None:
            return False
        now: float = time.monotonic()
        # Cost guard: no NEW image within VISION_MIN_INTERVAL_S of the last send
        # or twice in one turn. Doubles as the replay-turn path: the replayed
        # turn re-triggers look, lands here, and the model answers from the
        # frame that entered context with the replay.
        min_interval: float = config.REALTIME_GEMINI_VISION_MIN_INTERVAL_S
        since_last: float = now - self._last_look_sent_monotonic
        if self._looked_this_turn or (
            min_interval > 0
            and self._last_look_sent_monotonic > 0.0
            and since_last < min_interval
        ):
            reason: str = (
                "already looked this turn"
                if self._looked_this_turn
                else f"{since_last:.1f}s < {min_interval:.0f}s since last send"
            )
            logger.info(
                "[realtime] look: reusing recent frame (%s) — no new image sent (cost)",
                reason,
            )
            self._agent.send(
                [
                    FunctionCallResultInput(
                        call_id=output.call_id,
                        output='{"result": "using the current view; answer now"}',
                    )
                ]
            )
            return False

        frame = self._capture_frame()
        if frame is None:
            logger.warning("[realtime] look: no camera frame available")
            self._agent.send(
                [
                    FunctionCallResultInput(
                        call_id=output.call_id,
                        output='{"error": "camera unavailable"}',
                    )
                ]
            )
            return False

        self._agent.send([ImageInput(image=frame)])
        self._looked_this_turn = True
        self._last_look_sent_monotonic = now
        # Persist the SAME frame so that if this turn later delegates / falls
        # back to the main agent (e.g. Gemini times out mid-turn), the agent
        # reuses it instead of taking a fresh snapshot. See turn_dispatch.
        saved_path: str | None = self._persist_look_frame(frame)
        logger.info(
            "[realtime] look: captured frame %s in %.0fms → %s — replaying "
            "turn so the frame joins it",
            getattr(frame, "shape", "?"),
            (time.monotonic() - now) * 1000,
            saved_path or "(persist failed)",
        )
        return True

    @staticmethod
    def _capture_frame() -> Any:
        """Return the latest camera frame (BGR ndarray) downscaled for cost, or
        None if no camera. Reads HAL camera state in-process (no HTTP loopback);
        mirrors the wait/disable handling of routes.camera.camera_snapshot.

        Uses capture_still, so servos (animation loop + tracker worker) are
        frozen and the frame is guaranteed captured after the arm went quiet —
        motion blur made Gemini misread scenes. Costs up to ~settle+timeout
        only while the servos are actually moving; when they are still (or the
        device has none — animation_service is None) it returns immediately."""
        try:
            import cv2

            import hal.app_state as state
            from hal.devices.video_capture_device import capture_still
        except Exception:
            return None
        cap = getattr(state, "camera_capture", None)
        if cap is None:
            return None
        was_disabled: bool = bool(getattr(state, "_camera_disabled", False))
        try:
            if was_disabled:
                cap.start()
            try:
                frame: Any = capture_still(
                    cap,
                    getattr(state, "animation_service", None),
                    settle_s=0.3,
                    # 2.0s, not less: a camera woken from disabled (cap.start()
                    # above) can take over a second to deliver its first frame.
                    timeout_s=2.0,
                )
            finally:
                if was_disabled:
                    cap.stop()
            if frame is None:
                return None
            max_w: int = config.REALTIME_GEMINI_VISION_MAX_WIDTH
            h, w = frame.shape[:2]
            if max_w and w > max_w:
                scale: float = max_w / float(w)
                frame = cv2.resize(
                    frame,
                    (max(1, int(w * scale)), max(1, int(h * scale))),
                    interpolation=cv2.INTER_AREA,
                )
            return frame
        except Exception:
            logger.exception("[realtime] look: frame capture failed")
            return None

    @staticmethod
    def _persist_look_frame(frame: Any) -> str | None:
        """Save the look frame to disk and record it in app_state so a delegate /
        fallback turn can hand it to the main agent by path (see turn_dispatch).
        Best-effort: a write failure just means the agent snapshots fresh.
        Returns the saved path (also logged per capture for debugging: pull the
        file and compare it against the model's answer)."""
        try:
            import os
            import time as _time

            import cv2

            import hal.app_state as state

            os.makedirs(state._SNAPSHOT_DIR, exist_ok=True)
            path: str = os.path.join(
                state._SNAPSHOT_DIR, f"look_{int(_time.time() * 1000)}.jpg"
            )
            if not cv2.imwrite(path, frame):
                return None
            # Drop the previous look frame so they don't pile up (the snapshot ring
            # only tracks /camera/snapshot saves, not these).
            prev = getattr(state, "realtime_look_frame_path", None)
            if prev and prev != path and os.path.basename(prev).startswith("look_"):
                try:
                    os.remove(prev)
                except OSError:
                    pass
            state.realtime_look_frame_path = path
            state.realtime_look_frame_ts = time.monotonic()
            return path
        except Exception:
            logger.exception("[realtime] look: persist frame failed")
            return None

    def send_text(self, text: str) -> None:
        """Send a text message to the agent as context (non-blocking).

        Used to feed back results from the main system after delegation,
        so the agent knows what happened.
        """
        if (
            config.REALTIME_PROVIDER.strip().lower() == "gemini"
            and gemini_needs_idle_workaround()
        ):
            # 2.5 native-audio via google-genai is sensitive to non-response
            # clientContent turns (`turn_complete=False`) mixed with audio turns:
            # even when sent before audio, repeated context/TTS-history injections
            # can leave the session in a state that later closes with WS 1011.
            # Keep its live session on the same wire shape as the browser probe:
            # audio realtimeInput + tool responses only. 3.1 is NOT sensitive →
            # allow text context (so per-turn [TURN CONTEXT] reaches the model).
            logger.debug("[realtime] Gemini native-audio: skipping non-response text context")
            return
        if self._agent is not None:
            self._agent.send([TextInput(text=text)])

    def save_turn(self, user_text: str, agent_text: str) -> None:
        """Save a conversation turn to realtime memory."""
        self._context.add_turn(user_text, agent_text)

    def send_function_result(self, call_id: str, output: str) -> None:
        """Send a function call result back to the model."""
        if self._agent is not None:
            self._agent.send([FunctionCallResultInput(call_id=call_id, output=output)])
