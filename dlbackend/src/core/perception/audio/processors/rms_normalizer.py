"""RMS loudness normalization."""

import numpy as np

from core.models.media import Audio

from .base import AudioProcessorBase


class RMSNormalizer(AudioProcessorBase):
    """Scale waveform to a fixed RMS so enroll/query share the same loudness."""

    def __init__(self, target_rms: float = 0.1, max_gain: float = 20.0) -> None:
        super().__init__()
        self._target_rms: float = target_rms
        self._max_gain: float = max_gain

    def _process_impl(self, input: Audio) -> Audio:
        if input.waveform.shape[0] == 0:
            return input

        rms: float = float(np.sqrt(np.mean(input.waveform ** 2)))
        if rms < 1e-6:
            return input

        gain: float = min(self._target_rms / rms, self._max_gain)
        return Audio(
            waveform=(input.waveform * gain).astype(np.float32),
            sample_rate=input.sample_rate,
        )
