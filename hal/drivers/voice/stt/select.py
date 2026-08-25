"""Shared STT provider selection — one selection policy for both the boot
auto-start path (hal/server.py, reading os-server's config.json) and the
runtime path (hal/routes/voice.py POST /voice/start, reading
VoiceStartRequest). Keeping the policy in one place means a config change
(e.g. adding a provider) only needs one edit instead of two call sites
silently drifting apart.
"""

import logging
from typing import List, Optional

from hal.drivers.voice.stt.autonomous import AutonomousSTT
from hal.drivers.voice.stt.deepgram import DeepgramSTT
from hal.drivers.voice.stt.openai import OpenAISTT
from hal.drivers.voice.stt.provider import STTProvider

logger = logging.getLogger("hal.voice.stt")


def select_stt_provider(
    *,
    stt_provider: str = "",
    deepgram_api_key: str = "",
    llm_api_key: str = "",
    llm_base_url: str = "",
    stt_api_key: str = "",
    stt_base_url: str = "",
    stt_model: str = "",
    stt_language: str = "",
    keywords: Optional[List[str]] = None,
) -> Optional[STTProvider]:
    """Pick and construct an STT provider from config.

    `stt_provider`:
        ""           — legacy auto-selection: deepgram if deepgram_api_key is
                        set, else autonomous.
        "deepgram"   — force Deepgram (requires deepgram_api_key).
        "autonomous" — force AutonomousSTT (requires llm_api_key + llm_base_url).
        "openai"     — force OpenAISTT against any OpenAI-compatible server
                        (requires stt_base_url or llm_base_url).

    `stt_api_key` / `stt_base_url` fall back to `llm_api_key` / `llm_base_url`
    when empty, for households sharing one LLM credential across TTS/STT.
    """
    keywords = keywords or []
    provider = (stt_provider or "").strip().lower()
    resolved_key = stt_api_key or llm_api_key
    resolved_base = stt_base_url or llm_base_url

    def _deepgram() -> Optional[STTProvider]:
        if not deepgram_api_key:
            return None
        return DeepgramSTT(api_key=deepgram_api_key, keywords=keywords)

    def _autonomous() -> Optional[STTProvider]:
        if not (llm_api_key and llm_base_url):
            return None
        kwargs = {}
        if stt_model:
            kwargs["model"] = stt_model
        if stt_language:
            kwargs["language"] = stt_language
        return AutonomousSTT(
            api_key=llm_api_key, base_url=llm_base_url, keywords=keywords, **kwargs
        )

    def _openai() -> Optional[STTProvider]:
        if not resolved_base:
            return None
        kwargs = {}
        if stt_model:
            kwargs["model"] = stt_model
        if stt_language:
            kwargs["language"] = stt_language
        return OpenAISTT(
            api_key=resolved_key, base_url=resolved_base, keywords=keywords, **kwargs
        )

    if provider == "deepgram":
        return _deepgram()
    if provider == "autonomous":
        return _autonomous()
    if provider == "openai":
        return _openai()
    if provider:
        logger.warning("Unknown stt_provider=%r — falling back to legacy selection", provider)
    # Legacy default ("" or unrecognized): deepgram if configured, else autonomous.
    return _deepgram() or _autonomous()
