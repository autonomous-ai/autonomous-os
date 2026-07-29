"""On-device audio preprocessing pipeline for speaker recognition.

Ported from perception-service so the filter/VAD/normalize chain runs on HAL
(next to the mic) instead of on the embedding server. Audio that passes the gate
is sent to perception purely to compute the embedding; audio that fails raises
PreprocessRejected and never leaves the device.
"""

from .base import Audio, AudioProcessorBase
from .composite import CompositeAudioProcessor
from .exceptions import (
    REJECT_EMPTY_INPUT,
    REJECT_LOW_VOICE_RATIO,
    REJECT_TOO_SHORT,
    REJECT_VAD_REMOVED_ALL,
    PreprocessRejected,
)
from .factory import AudioProcessorFactory

__all__ = [
    "Audio",
    "AudioProcessorBase",
    "AudioProcessorFactory",
    "CompositeAudioProcessor",
    "PreprocessRejected",
    "REJECT_EMPTY_INPUT",
    "REJECT_VAD_REMOVED_ALL",
    "REJECT_TOO_SHORT",
    "REJECT_LOW_VOICE_RATIO",
]
