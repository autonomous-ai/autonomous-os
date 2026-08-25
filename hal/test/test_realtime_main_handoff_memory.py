"""Regression coverage for main-agent handoffs surviving a Gemini session swap."""

from hal.realtime.orchestrator import RealtimeOrchestrator


class _RecordingContext:
    def __init__(self) -> None:
        self.turns: list[tuple[str, str]] = []

    def add_turn(self, user_text: str, agent_text: str) -> None:
        self.turns.append((user_text, agent_text))


def _orchestrator() -> tuple[RealtimeOrchestrator, _RecordingContext]:
    context = _RecordingContext()
    orchestrator = object.__new__(RealtimeOrchestrator)
    orchestrator._context = context
    return orchestrator, context


def test_main_handoff_persists_the_user_request_before_the_reply_arrives():
    orchestrator, context = _orchestrator()

    orchestrator.save_main_handoff("Remind me what I asked about my calendar")

    assert context.turns == [
        (
            "Remind me what I asked about my calendar",
            "[This request was handed to the main agent; its spoken reply follows.]",
        )
    ]


def test_main_agent_tts_fragments_are_persisted_instead_of_only_sent_to_live_session():
    orchestrator, context = _orchestrator()

    orchestrator.save_main_agent_reply_fragment("You have a meeting at two.")
    orchestrator.save_main_agent_reply_fragment("  ")
    orchestrator.save_main_agent_reply_fragment("It lasts thirty minutes.")

    assert context.turns == [
        ("[Main agent reply]", "You have a meeting at two."),
        ("[Main agent reply]", "It lasts thirty minutes."),
    ]
