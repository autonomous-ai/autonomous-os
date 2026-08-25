"""Cascaded realtime brain — driving the LLM half of a voice turn from text.

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
from hal.realtime.config import _load_language
from hal.realtime.context_manager import ContextManagerBase
from hal.realtime.enums import AgentGateway
from hal.realtime.orchestrator import (
    DEFAULT_SAMPLE_RATE,
    DELEGATE_TOOL,
    EMOTION_TOOL,
    LOOK_TOOL,
    RealtimeOrchestrator,
    _camera_present,
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
        provider: str = "pipecat",
    ) -> None:
        self._expression_enabled = enable_expression
        # "pipecat" drives the turn through the pipecat framework; "cascaded"
        # is the same contract over a plain OpenAI-compatible client, with no
        # framework and no pipecat dependency. Both read realtime.pipecat.*.
        self._provider = (provider or "pipecat").strip().lower()
        self._agent = None
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
        # In-session vision, cascaded only: the image-in-context flow lives in
        # CascadedAgent (ImageContext). Legacy pipecat keeps delegating visual
        # questions to main, same as it does today. `_camera_present` is the
        # same "can this device see" signal RealtimeOrchestrator gates on.
        if self._provider == "cascaded" and _camera_present():
            tools.append(_as_host_tool(LOOK_TOOL))
        agent_cls, config_cls = self._agent_classes()
        agent = agent_cls(
            config_cls(
                base_url=config.REALTIME_PIPECAT_BASE_URL,
                api_key=config.REALTIME_PIPECAT_API_KEY or "not-needed",
                model=config.REALTIME_PIPECAT_MODEL,
                gemini_api_key=config.REALTIME_PIPECAT_SEARCH_KEY,
                search_base_url=config.REALTIME_PIPECAT_SEARCH_BASE_URL,
            ),
            instructions=self._context.build_instructions(),
            host_tools=tools,
        )
        if not agent.start():
            logger.warning(
                "[%s] start failed — falling back to the main agent", self._provider
            )
            return
        self._agent = agent
        logger.info(
            "[%s] ready — model=%s base_url=%s",
            self._provider,
            config.REALTIME_PIPECAT_MODEL,
            config.REALTIME_PIPECAT_BASE_URL,
        )

    def _agent_classes(self):
        """(agent, config) classes for this provider. Imported lazily so the
        cascaded path never pulls in pipecat."""
        if self._provider == "cascaded":
            from hal.realtime.voice_agent.openai_cascaded import (
                CascadedAgent,
                CascadedConfig,
            )

            return CascadedAgent, CascadedConfig
        from hal.pipecat_rt import PipecatAgent, PipecatConfig

        return PipecatAgent, PipecatConfig

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

    def tool_result(
        self, call_id: str, output: str, run_llm: bool = True, image=None
    ) -> None:
        if self._agent is None:
            return
        # `image` is CascadedAgent-only (see openai_cascaded.CascadedAgent.
        # tool_result); the legacy PipecatAgent signature has no such kwarg,
        # so it must never receive it — not even as None. `look` is only ever
        # registered for provider="cascaded" (see start()), so this branch is
        # the only caller that would ever pass one.
        if image is not None:
            self._agent.tool_result(call_id, output, run_llm=run_llm, image=image)
        else:
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
