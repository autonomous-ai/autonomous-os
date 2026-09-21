"""Pin Gemini's filler/handoff contract; model compliance needs device trials."""

from hal.realtime.constants import RESOURCES_DIR


PROMPT = (RESOURCES_DIR / "system_prompt_gemini.md").read_text(encoding="utf-8")


def test_acknowledgment_never_replaces_same_turn_delegation():
    assert "may immediately speak one brief acknowledgment" in PROMPT
    assert "MUST call `delegate_to_main` in the SAME turn" in PROMPT
    assert "Do not wait for the main agent or a tool result" in PROMPT
    assert "never end the turn after the acknowledgment alone" in PROMPT
    assert "never say the action is done, music is playing, or a reminder is set" in PROMPT
    for obsolete_rule in (
        "Call the tool OR speak", "No filler or meta-commentary",
        "DELEGATE (empty voice output)", "MUST delegate with blank voice",
        "your spoken output must be completely blank",
    ):
        assert obsolete_rule not in PROMPT
    delegation_examples = [line for line in PROMPT.splitlines()
                           if line.startswith("User:") and "delegate_to_main(" in line]
    assert delegation_examples
    assert all("blank voice" not in line for line in delegation_examples)


def test_music_example_requires_tool_call_after_immediate_receipt():
    example = next(line for line in PROMPT.splitlines()
                   if line.startswith('User: "Play something light'))
    assert 'immediately say "I can help with that." AND call' in example
    assert 'delegate_to_main(message="Play something light, don\'t make it too loud")' in example
    assert "in the SAME turn" in example
    assert "never stop there or claim music is already playing" in example


def test_rejection_stays_silent_even_when_delegation_allows_acknowledgment():
    assert "For `reject_turn`, voice and text MUST remain completely blank" in PROMPT
    assert "rejection never gets an acknowledgment" in PROMPT
    assert "`reject_turn` when available + completely blank voice/text; no acknowledgment" in PROMPT
