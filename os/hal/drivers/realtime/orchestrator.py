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
from hal.drivers.realtime.config import GeminiConfig, OpenAIConfig, _load_language
from hal.drivers.realtime.context_manager import (
    ContextManagerBase,
    HermesContextManager,
    OpenClawContextManager,
)
from hal.drivers.realtime.enums import AgentGateway
from hal.drivers.realtime.models import (
    FunctionCallOutput,
    FunctionCallResultInput,
    OutputBase,
    TextInput,
)
from hal.drivers.realtime.models.signal import DelegateSignal
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
        self._tools: list[dict[str, Any]] = tools + (extra_tools or [])
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

    def stream_output(self) -> Generator[OutputBase | DelegateSignal, None, None]:
        """Yield outputs from the model one by one as they arrive.

        Yields:
          - AudioOutput / TextOutput / FunctionCallOutput as they stream in
          - DelegateSignal if model called delegate_to_main (then stops)

        The generator returns (StopIteration) when the model's turn is done.
        """
        if self._agent is None:
            return

        produced = False  # did this turn yield any real output (vs stay silent)?
        for output in self._agent.receive(stop_on_done=True):
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
                yield DelegateSignal(message=delegate_msg)
                continue
            produced = True
            yield output

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

    def send_text(self, text: str) -> None:
        """Send a text message to the agent as context (non-blocking).

        Used to feed back results from the main system after delegation,
        so the agent knows what happened.
        """
        if self._agent is not None:
            self._agent.send([TextInput(text=text)])

    def save_turn(self, user_text: str, agent_text: str) -> None:
        """Save a conversation turn to realtime memory."""
        self._context.add_turn(user_text, agent_text)

    def send_function_result(self, call_id: str, output: str) -> None:
        """Send a function call result back to the model."""
        if self._agent is not None:
            self._agent.send([FunctionCallResultInput(call_id=call_id, output=output)])
