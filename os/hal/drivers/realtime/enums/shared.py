"""Shared enumerations used by both providers."""

from enum import StrEnum


class AgentGateway(StrEnum):
    OPENCLAW = "openclaw"
    HERMES = "hermes"


class InputTypeEnum(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    FUNCTION_CALL_RESULT = "function_call_result"


class OutputTypeEnum(StrEnum):
    TEXT = "text"
    AUDIO = "audio"
    FUNCTION_CALL = "function_call"


class InputEventTypeEnum(StrEnum):
    """Types for the agent send queue."""
    INPUT = "input"
    AUDIO_COMMIT = "audio_commit"


class OutputEventTypeEnum(StrEnum):
    """Types for the agent receive queue."""
    OUTPUT = "output"
    TURN_DONE = "turn_done"
