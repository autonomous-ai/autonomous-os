"""Audio processing utilities for endpoints."""

from __future__ import annotations

import base64
import io

import numpy as np
import soundfile as sf

from config import settings
from core.models.media import Audio


def decode_b64_wav(b64: str) -> Audio:
    """Decode a base64-encoded WAV into an Audio dataclass."""
    raw = base64.b64decode(b64)
    if not raw:
        raise ValueError("empty audio payload")
    max_bytes = settings.input_limits.max_audio_bytes
    if len(raw) > max_bytes:
        raise ValueError(f"Audio exceeds {max_bytes // (1024 * 1024)} MB limit")
    try:
        waveform, sample_rate = sf.read(io.BytesIO(raw), dtype="float32")
    except Exception:
        raise ValueError("Unsupported or corrupt audio file")
    arr = np.asarray(waveform, dtype=np.float32)
    if arr.ndim == 2:
        arr = arr.mean(axis=1)
    elif arr.ndim != 1:
        raise ValueError("Uploaded wav must be mono/stereo waveform.")
    max_duration = settings.input_limits.max_audio_duration_s
    duration = len(arr) / sample_rate
    if duration > max_duration:
        raise ValueError(f"Audio duration {duration:.1f}s exceeds {max_duration:.0f}s limit")
    return Audio(waveform=arr, sample_rate=int(sample_rate))
