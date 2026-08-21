"""Cascaded realtime brain — pipecat driving the LLM half of a voice turn.

The audio-native providers (gemini/openai/qwen) take microphone frames and
return speech. This one does not: HAL keeps its own STT and TTS, and pipecat
owns only transcript -> LLM + tools -> text. It therefore shares nothing with
`voice_agent/` and never becomes a `VoiceAgentBase`.

It does duck-type the slice of `RealtimeOrchestrator` that `voice_service`
touches during capture, so only the turn driver has to branch. The audio
methods are deliberate no-ops — this brain is fed the finished transcript.
"""

import logging
from typing import Any, Generator

from hal import config
from hal.pipecat_rt import PipecatAgent, PipecatConfig
from hal.realtime.config import _load_language
from hal.realtime.context_manager import ContextManagerBase
from hal.realtime.enums import AgentGateway
from hal.realtime.orchestrator import (
    DEFAULT_SAMPLE_RATE,
    DELEGATE_TOOL,
    EMOTION_TOOL,
    RealtimeOrchestrator,
)
from hal.realtime.summarizer import RealtimeSummarizer

logger = logging.getLogger("hal.realtime")


def _as_host_tool(tool: dict[str, Any]) -> tuple[str, str, dict]:
    """Convert a realtime tool schema to pipecat's (name, description, params)."""
    return tool["name"], tool["description"], tool.get("parameters", {})


class PipecatSession:
    """One pipecat conversation, with the same persona/memory context the
    audio-native providers get.

    Instructions are rebuilt every REALTIME_SESSION_MAX_TURNS turns so a rename
    or new memory lands; between rebuilds the prompt is byte-stable, which is
    what keeps the gateway's prefix cache warm.
    """

    def __init__(
        self,
        gateway: AgentGateway = AgentGateway.OPENCLAW,
        enable_expression: bool = False,
    ) -> None:
        self._expression_enabled = enable_expression
        self._agent: PipecatAgent | None = None
        self._turns_since_rebuild = 0

        summarizer: RealtimeSummarizer | None = None
        if config.REALTIME_SUMMARIZER_ENABLED:
            try:
                summarizer = RealtimeSummarizer()
            except Exception as e:
                logger.warning("[pipecat] summarizer unavailable: %s", e)
        context_cls = RealtimeOrchestrator.CONTEXT_MANAGERS.get(gateway)
        if context_cls is None:
            from hal.realtime.context_manager import OpenClawContextManager

            context_cls = OpenClawContextManager
        self._context: ContextManagerBase = context_cls(
            workspace_dir=RealtimeOrchestrator.WORKSPACE_DIRS.get(
                gateway, config.OPENCLAW_WORKSPACE_DIR
            ),
            language=_load_language() or "English",
            provider="pipecat",
            summarizer=summarizer,
        )

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if not config.REALTIME_PIPECAT_BASE_URL or not config.REALTIME_PIPECAT_MODEL:
            logger.warning(
                "[pipecat] disabled — base_url=%r model=%r (set realtime.pipecat.*)",
                config.REALTIME_PIPECAT_BASE_URL,
                config.REALTIME_PIPECAT_MODEL,
            )
            return
        tools = [_as_host_tool(DELEGATE_TOOL)]
        if self._expression_enabled:
            tools.append(_as_host_tool(EMOTION_TOOL))
        agent = PipecatAgent(
            PipecatConfig(
                base_url=config.REALTIME_PIPECAT_BASE_URL,
                api_key=config.REALTIME_PIPECAT_API_KEY or "not-needed",
                model=config.REALTIME_PIPECAT_MODEL,
                gemini_api_key=config.REALTIME_PIPECAT_SEARCH_KEY,
            ),
            instructions=self._context.build_instructions(),
            host_tools=tools,
        )
        if not agent.start():
            logger.warning("[pipecat] start failed — falling back to the main agent")
            return
        self._agent = agent
        logger.info(
            "[pipecat] ready — model=%s base_url=%s",
            config.REALTIME_PIPECAT_MODEL,
            config.REALTIME_PIPECAT_BASE_URL,
        )

    def stop(self) -> None:
        agent, self._agent = self._agent, None
        if agent is not None:
            agent.stop()
        try:
            self._context.summarize_device_memory()
            self._context.summarize_realtime_memory()
        except Exception:
            logger.exception("[pipecat] memory summarization failed on shutdown")

    # --- orchestrator-shaped surface used by voice_service -----------------

    @property
    def available(self) -> bool:
        return self._agent is not None and self._agent.available

    @property
    def sample_rate(self) -> int:
        return DEFAULT_SAMPLE_RATE

    @property
    def rebuilding(self) -> bool:
        return False

    def wait_until_available(self, timeout_s: float = 2.0) -> bool:
        return self.available

    def prepare_turn(self) -> None:
        """No session to warm: the HTTP client is pooled and the prompt cached."""

    def append_audio(self, frame: bytes) -> None:
        """No-op — the transcript, not the audio, reaches this brain."""

    def send_text(self, text: str) -> None:
        if self._agent is not None:
            self._agent.add_context(text)

    def save_turn(self, user_text: str, agent_text: str) -> None:
        self._context.add_turn(user_text, agent_text)

    # --- turn --------------------------------------------------------------

    def run_turn(self, transcript: str) -> Generator[Any, None, None]:
        if self._agent is None:
            return iter(())
        return self._agent.run_turn(transcript)

    def tool_result(self, call_id: str, output: str, run_llm: bool = True) -> None:
        if self._agent is not None:
            self._agent.tool_result(call_id, output, run_llm=run_llm)

    def abort_turn(self) -> None:
        if self._agent is not None:
            self._agent.abort_turn()

    @property
    def last_metrics(self):
        return self._agent.last_metrics if self._agent is not None else None

    def turn_finished(self) -> None:
        """Count the turn and rebuild instructions once the cap is reached."""
        self._turns_since_rebuild += 1
        cap: int = config.REALTIME_SESSION_MAX_TURNS
        if cap <= 0 or self._turns_since_rebuild < cap or self._agent is None:
            return
        self._turns_since_rebuild = 0
        try:
            self._agent.rebuild_context(self._context.build_instructions())
            logger.info("[pipecat] context rebuilt after %d turns", cap)
        except Exception as e:
            logger.warning("[pipecat] context rebuild failed: %s", e)
