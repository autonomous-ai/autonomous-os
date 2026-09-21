"""Regression: extended-thinking needs NON_BLOCKING tool declarations.

`gemini-3.8-live-extended-thinking` accepts ONLY NON_BLOCKING tool declarations;
a BLOCKING one makes it error mid-turn and speak a canned "I'm sorry, an error
occurred." after every tool call (device-observed 2026-09-17). Every other live
model keeps the BLOCKING default. The behavior is gated on the model string in
GeminiLiveAgent._build_config.
"""

from types import SimpleNamespace

from google.genai import types

from hal.realtime.enums.gemini import GeminiThinkingLevel
from hal.realtime.voice_agent.gemini_live import GeminiLiveAgent


def _build(model: str) -> types.LiveConnectConfig:
    agent = object.__new__(GeminiLiveAgent)
    agent._vad_disabled = True  # short-circuits _activity_detection setup
    agent._resumption_handle = None
    agent._tools = [{"name": "delegate_to_main", "description": "", "parameters": None}]
    agent._config = SimpleNamespace(
        language=None,
        use_language_codes=False,
        model=model,
        thinking_level=GeminiThinkingLevel.LOW,
        voice=SimpleNamespace(value="Kore"),
        instructions="sys",
        google_search_enabled=False,
        session_resumption_enabled=False,
    )
    return agent._build_config()


def _tool_behavior(cfg: types.LiveConnectConfig):
    return cfg.tools[0].function_declarations[0].behavior


def test_extended_thinking_declares_non_blocking() -> None:
    cfg = _build("gemini-3.8-live-extended-thinking")
    declarations = cfg.tools[0].function_declarations
    assert {tool.name for tool in declarations} == {"delegate_to_main", "complete_response"}
    assert all(tool.behavior == types.Behavior.NON_BLOCKING for tool in declarations)
    completion, = [tool for tool in declarations if tool.name == "complete_response"]
    assert completion.parameters.required in (None, [])
    assert "Never use for an action" in completion.description


def test_plain_live_leaves_tool_blocking() -> None:
    # Unset behavior == the provider's BLOCKING default; must NOT be NON_BLOCKING.
    for model in ("gemini-3.8-live", "gemini-3.1-flash-live-preview"):
        cfg = _build(model)
        assert _tool_behavior(cfg) != types.Behavior.NON_BLOCKING
        assert [tool.name for tool in cfg.tools[0].function_declarations] == ["delegate_to_main"]


if __name__ == "__main__":
    test_extended_thinking_declares_non_blocking()
    test_plain_live_leaves_tool_blocking()
    print("ok")
