"""Enumerations for the realtime voice agent service."""

from hal.realtime.enums.gemini import GeminiThinkingLevel, GeminiVoice
from hal.realtime.enums.gptlive import GPTLiveVoice
from hal.realtime.enums.openai import (
    OpenAIReasoningEffort,
    OpenAITruncationType,
    OpenAITurnDetectionType,
    OpenAIVoice,
)
from hal.realtime.enums.shared import (
    AgentGateway,
    InputEventTypeEnum,
    InputTypeEnum,
    OutputEventTypeEnum,
    OutputTypeEnum,
)

__all__ = [
    "AgentGateway",
    "InputTypeEnum",
    "OutputTypeEnum",
    "InputEventTypeEnum",
    "OutputEventTypeEnum",
    "OpenAITurnDetectionType",
    "OpenAIReasoningEffort",
    "OpenAITruncationType",
    "OpenAIVoice",
    "GeminiThinkingLevel",
    "GeminiVoice",
    "GPTLiveVoice",
]
