"""Speech-to-text (STT) package: provider ABC + pluggable engines.

Public surface:
    STTProvider / STTSession — abstract provider + session contract
    AutonomousSTT            — autonomous (self-hosted) engine
    DeepgramSTT              — Deepgram engine
"""

from hal.drivers.voice.stt.autonomous import AutonomousSTT
from hal.drivers.voice.stt.deepgram import DeepgramSTT
from hal.drivers.voice.stt.provider import STTProvider, STTSession

__all__ = [
    "AutonomousSTT",
    "DeepgramSTT",
    "STTProvider",
    "STTSession",
]
