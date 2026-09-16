"""Exceptions for realtime voice agent providers."""


class OpenAIRealtimeError(Exception):
    """Raised on OpenAI Realtime API errors."""


class GeminiLiveError(Exception):
    """Raised on Gemini Live API errors (e.g. go_away)."""
