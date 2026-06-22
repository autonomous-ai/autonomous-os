"""Audio conversion utilities for PCM16 codec."""

import base64
from math import gcd

import numpy as np
import numpy.typing as npt


def float32_to_base64_pcm16(audio: npt.NDArray[np.float32]) -> str:
    """Convert float32 audio [-1.0, 1.0] to base64-encoded PCM16. Used by OpenAI."""
    clipped = np.clip(audio, -1.0, 1.0)
    pcm16 = (clipped * 32767).astype(np.int16)
    return base64.b64encode(pcm16.tobytes()).decode()


def base64_pcm16_to_float32(b64_audio: str) -> npt.NDArray[np.float32]:
    """Convert base64-encoded PCM16 to float32 [-1.0, 1.0]. Used by OpenAI."""
    return pcm16_bytes_to_float32(base64.b64decode(b64_audio))


def float32_to_pcm16_bytes(audio: npt.NDArray[np.float32]) -> bytes:
    """Convert float32 audio [-1.0, 1.0] to raw PCM16 bytes. Used by Gemini."""
    clipped = np.clip(audio, -1.0, 1.0)
    pcm16 = (clipped * 32767).astype(np.int16)
    return pcm16.tobytes()


def pcm16_bytes_to_float32(data: bytes) -> npt.NDArray[np.float32]:
    """Convert raw PCM16 bytes to float32 [-1.0, 1.0]. Used by Gemini.

    Trims a trailing odd byte before decoding: streamed audio chunks can split
    mid-sample, and np.frombuffer(dtype=int16) requires an even length (2 bytes
    per sample) — an odd buffer otherwise raises "buffer size must be a multiple
    of element size" and kills the recv loop. Dropping the half-sample is
    inaudible.
    """
    usable = len(data) - (len(data) % 2)
    if usable <= 0:
        return np.empty(0, dtype=np.float32)
    pcm16 = np.frombuffer(data[:usable], dtype=np.int16)
    return (pcm16.astype(np.float32) / 32767.0)


def resample_float32(
    audio: npt.NDArray[np.float32], src_rate: int, dst_rate: int,
) -> npt.NDArray[np.float32]:
    """Resample float32 audio from src_rate to dst_rate. No-op if rates match."""
    if src_rate == dst_rate:
        return audio
    import scipy.signal
    g = gcd(dst_rate, src_rate)
    return scipy.signal.resample_poly(audio, dst_rate // g, src_rate // g).astype(np.float32)
