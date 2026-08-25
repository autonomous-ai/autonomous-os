"""Speech-to-text (STT) package: provider ABC + pluggable engines.

Public surface:
    STTProvider / STTSession — abstract provider + session contract
    AutonomousSTT            — autonomous (self-hosted) engine
    DeepgramSTT              — Deepgram engine
    OpenAISTT                — any OpenAI-compatible /audio/transcriptions engine
    select_stt_provider      — shared provider-selection policy (server.py + routes/voice.py)
"""

from hal.drivers.voice.stt.autonomous import AutonomousSTT
from hal.drivers.voice.stt.deepgram import DeepgramSTT
from hal.drivers.voice.stt.openai import OpenAISTT
from hal.drivers.voice.stt.provider import STTProvider, STTSession
from hal.drivers.voice.stt.select import select_stt_provider

__all__ = [
    "AutonomousSTT",
    "DeepgramSTT",
    "OpenAISTT",
    "STTProvider",
    "STTSession",
    "select_stt_provider",
]
