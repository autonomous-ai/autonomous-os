"""
TTS Backend abstraction — pluggable providers for text-to-speech streaming.

Supported providers:
  - openai (default): OpenAI-compatible API (works with any OpenAI-compatible server)
  - elevenlabs: ElevenLabs TTS API with streaming support
"""

import logging
from abc import ABC, abstractmethod
from typing import Iterator, Optional

logger = logging.getLogger("hal.voice.tts")

# Provider constants
PROVIDER_OPENAI = "openai"
PROVIDER_ELEVENLABS = "elevenlabs"

# All backends output 24kHz 16-bit mono PCM
TTS_SAMPLE_RATE = 24000
STREAM_CHUNK_SIZE = 4096


class TTSBackend(ABC):
    """Abstract TTS backend — streams raw PCM int16 bytes from text."""

    @abstractmethod
    def stream_pcm(
        self,
        text: str,
        voice: str,
        model: str,
        speed: float,
        instructions: Optional[str] = None,
    ) -> Iterator[bytes]:
        """Yield raw PCM int16 byte chunks (24kHz mono) for the given text."""
        ...

    @property
    def sample_rate(self) -> int:
        return TTS_SAMPLE_RATE

    @property
    def volume_boost(self) -> float:
        """Volume multiplier applied to PCM samples. Override per provider."""
        return 2.5

    @property
    @abstractmethod
    def available(self) -> bool:
        ...


def create_backend(
    provider: str,
    api_key: str,
    base_url: str = "",
) -> TTSBackend:
    """Factory: create a TTS backend by provider name."""
    provider = (provider or PROVIDER_OPENAI).lower().strip()
    if provider == PROVIDER_ELEVENLABS:
        from hal.drivers.voice.tts.elevenlabs import ElevenLabsTTSBackend
        return ElevenLabsTTSBackend(api_key=api_key, base_url=base_url)
    # Default: openai-compatible
    from hal.drivers.voice.tts.openai import OpenAITTSBackend
    return OpenAITTSBackend(api_key=api_key, base_url=base_url)
