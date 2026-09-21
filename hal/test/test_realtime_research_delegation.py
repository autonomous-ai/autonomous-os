"""Regression guard for realtime routing of multi-step work.

Every realtime provider prompt, and the shared delegate tool description, must
send research / analysis / brainstorming / report requests to the main agent
instead of answering (or search-grounding) them in-session. Gemini's Google
Search grounding is for ONE fresh fact; a customer-research question answered
in two spoken sentences is the failure this test pins down.
"""
import pytest

from hal.realtime.constants import RESOURCES_DIR
from hal.realtime.orchestrator import DELEGATE_TOOL_DESCRIPTION

PROMPTS = [
    "system_prompt.md",
    "system_prompt_openai.md",
    "system_prompt_gemini.md",
    "system_prompt_pipecat.md",
]


@pytest.mark.parametrize("name", PROMPTS)
def test_prompt_delegates_research_analysis_and_documents(name):
    text = (RESOURCES_DIR / name).read_text(encoding="utf-8")
    assert "**Research, analysis & documents:**" in text
    assert "brainstorm" in text
    assert "report" in text


# The prompt that can search in-session: the boundary has to be stated there,
# next to the grounding rule itself.
def test_search_grounding_bullet_excludes_multi_step_work():
    text = (RESOURCES_DIR / "system_prompt_gemini.md").read_text(encoding="utf-8")
    assert "Single facts only" in text


def test_delegate_tool_description_covers_research_and_documents():
    lowered = DELEGATE_TOOL_DESCRIPTION.lower()
    for word in ("research", "brainstorm", "report"):
        assert word in lowered
