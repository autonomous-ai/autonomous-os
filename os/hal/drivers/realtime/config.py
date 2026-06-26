"""Configuration for realtime voice agent providers.

All values are read from hal.config (environment variables).
"""

from pydantic import BaseModel

import hal.config as app_config
from hal.drivers.realtime.enums import (
    GeminiThinkingLevel,
    GeminiVoice,
    OpenAIReasoningEffort,
    OpenAITruncationType,
    OpenAITurnDetectionType,
    OpenAIVoice,
)


def _load_language() -> str | None:
    """Load language from the device's config.json (stt_language field)."""
    from hal.config import _os_cfg_get

    lang: str = _os_cfg_get("stt_language", "").strip()
    return lang if lang else None


def _parse_turn_detection(value: str) -> OpenAITurnDetectionType | None:
    """Parse HAL_REALTIME_TURN_DETECTION into an OpenAITurnDetectionType or None (off)."""
    v = value.strip().lower()
    if v in ("off", "none", ""):
        return None
    try:
        return OpenAITurnDetectionType(v)
    except ValueError:
        return OpenAITurnDetectionType.SERVER_VAD


class OpenAIConfig(BaseModel):
    api_key: str = app_config.REALTIME_OPENAI_API_KEY
    base_url: str | None = app_config.REALTIME_OPENAI_BASE_URL or None
    model: str = app_config.REALTIME_OPENAI_MODEL
    voice: OpenAIVoice = OpenAIVoice(app_config.REALTIME_OPENAI_VOICE)
    instructions: str = ""
    sample_rate: int = app_config.REALTIME_OPENAI_SAMPLE_RATE
    language: str | None = _load_language()
    turn_detection_type: OpenAITurnDetectionType | None = _parse_turn_detection(
        app_config.REALTIME_TURN_DETECTION
    )
    reasoning_effort: OpenAIReasoningEffort = OpenAIReasoningEffort(
        app_config.REALTIME_OPENAI_REASONING_EFFORT
    )
    truncation_type: OpenAITruncationType = OpenAITruncationType.RETENTION_RATIO
    truncation_retention_ratio: float = 0.5
    max_retries: int = 3
    reconnect_delay_s: float = 2.0


class GeminiConfig(BaseModel):
    api_key: str = app_config.REALTIME_GEMINI_API_KEY
    base_url: str | None = app_config.REALTIME_GEMINI_BASE_URL or None
    model: str = app_config.REALTIME_GEMINI_MODEL
    voice: GeminiVoice = GeminiVoice(app_config.REALTIME_GEMINI_VOICE)
    instructions: str = ""
    sample_rate: int = app_config.REALTIME_GEMINI_SAMPLE_RATE
    language: str | None = _load_language()
    use_language_codes: bool = app_config.REALTIME_GEMINI_USE_LANGUAGE_CODES
    session_resumption_enabled: bool = app_config.REALTIME_GEMINI_SESSION_RESUMPTION
    google_search_enabled: bool = app_config.REALTIME_GEMINI_GOOGLE_SEARCH
    thinking_level: GeminiThinkingLevel = GeminiThinkingLevel(
        app_config.REALTIME_GEMINI_THINKING_LEVEL
    )
    vad_enabled: bool = app_config.REALTIME_TURN_DETECTION.strip().lower() not in (
        "off",
        "none",
        "",
    )
    context_window_compression: bool = True
    compression_trigger_tokens: int = app_config.REALTIME_GEMINI_COMPRESSION_TRIGGER_TOKENS
    compression_target_tokens: int = app_config.REALTIME_GEMINI_COMPRESSION_TARGET_TOKENS
    keepalive_interval_s: float = app_config.REALTIME_GEMINI_KEEPALIVE_S
    max_retries: int = 3
    reconnect_delay_s: float = 2.0
    send_timeout_s: float = 10.0
    recv_timeout_s: float = 300.0
    queue_poll_s: float = 1.0
    join_timeout_s: float = 5.0
