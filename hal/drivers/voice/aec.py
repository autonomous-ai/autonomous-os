"""Acoustic echo cancellation for the mic path (WebRTC AEC3).

Provider-independent: the reference is tapped at the TTS output stream, so it
covers synthesized speech, queued speech and realtime native audio alike.

Requires `aec-audio-processing` (SWIG binding over libwebrtc-audio-processing-2).
It is NOT a hal dependency — absent or unloadable, every entry point here
degrades to a no-op and the voice path behaves exactly as before.

Wiring:
    tts/service.py  _WatchedStream.write  → reference_write()
    voice_service.py mic open             → wrap_mic()
"""

import logging
import os
import threading
import time
from math import gcd

logger = logging.getLogger("hal.voice.aec")

FRAME_MS = 10  # APM's fixed frame size
SUPPORTED_RATES = (8000, 16000, 32000, 48000)

_lock = threading.Lock()
_canceller = None
_reference = None
_unavailable_logged = False


class EchoReference:
    """FIFO of audio handed to the speaker, drained by the canceller.

    Bounded to `max_ms`: older audio is past any useful alignment and letting it
    pile up drifts the reference out of step with the mic.
    """

    def __init__(self, rate: int, max_ms: int = 500):
        self._buffer = bytearray()
        self._max_bytes = int(rate * 2 * max_ms / 1000)
        self._lock = threading.Lock()
        self._last_write = 0.0

    def write(self, pcm: bytes) -> None:
        with self._lock:
            self._buffer.extend(pcm)
            if len(self._buffer) > self._max_bytes:
                del self._buffer[: len(self._buffer) - self._max_bytes]
            self._last_write = time.monotonic()

    def read(self, nbytes: int) -> bytes:
        """Take the next `nbytes` of played audio, zero-padded if it ran dry."""
        with self._lock:
            if len(self._buffer) >= nbytes:
                out = bytes(self._buffer[:nbytes])
                del self._buffer[:nbytes]
                return out
            out = bytes(self._buffer) + b"\x00" * (nbytes - len(self._buffer))
            self._buffer.clear()
            return out

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()

    def idle_for(self) -> float:
        """Seconds since the last speaker write (inf if nothing was ever played)."""
        with self._lock:
            if self._last_write <= 0.0:
                return float("inf")
            return time.monotonic() - self._last_write


class EchoCanceller:
    """Runs mic audio through WebRTC's APM with played audio as the reference.

    `process()` is the only hot path: it buffers to APM's fixed 10 ms frames and
    returns exactly as many samples as the caller asked for, so it drops into an
    existing read loop without changing framing.
    """

    def __init__(self, rate: int, reference: EchoReference, delay_ms: int,
                 noise_suppression: bool, dump_dir: str = ""):
        self._rate = rate
        self._reference = reference
        self._frame_bytes = int(rate * FRAME_MS / 1000) * 2
        self._pending = bytearray()
        self._out = bytearray()
        self._apm = None
        self._dump = None
        self._erle_log_at = 0.0
        self._erle_mic = 0.0
        self._erle_out = 0.0
        self._erle_frames = 0

        from aec_audio_processing import AudioProcessor

        self._apm = AudioProcessor(
            enable_aec=True,
            enable_ns=noise_suppression,
            enable_agc=False,  # AGC rides the gain up under the bot's own voice
            enable_vad=False,  # hal runs webrtcvad/silero/ten-vad for that
        )
        self._apm.set_stream_format(rate, 1)
        self._apm.set_reverse_stream_format(rate, 1)
        self._apm.set_stream_delay(delay_ms)
        logger.info(
            "AEC3 active: %dHz, %dms frames, delay hint %dms, ns=%s",
            rate, FRAME_MS, delay_ms, noise_suppression,
        )
        if dump_dir:
            self._open_dump(dump_dir, rate)

    def reset(self) -> None:
        """Drop buffered audio after a route change or stream reopen."""
        self._pending.clear()
        self._out.clear()
        self._reference.clear()

    def process(self, pcm: bytes) -> bytes:
        """Cancel the speaker signal out of `pcm`, returning the same byte count.

        Primes with up to one 10 ms frame of silence on the first call; from then
        on input and output stay length-for-length.
        """
        want = len(pcm)
        self._pending.extend(pcm)
        while len(self._pending) >= self._frame_bytes:
            mic = bytes(self._pending[: self._frame_bytes])
            del self._pending[: self._frame_bytes]
            played = self._reference.read(self._frame_bytes)
            self._apm.process_reverse_stream(played)
            cleaned = self._apm.process_stream(mic)
            self._out.extend(cleaned)
            self._accumulate_erle(mic, cleaned, played)
            if self._dump:
                self._dump["mic"].writeframes(mic)
                self._dump["ref"].writeframes(played)
                self._dump["out"].writeframes(cleaned)
        if len(self._out) < want:
            self._out[:0] = b"\x00" * (want - len(self._out))
        out = bytes(self._out[:want])
        del self._out[:want]
        return out

    def close(self) -> None:
        self._apm = None
        if self._dump:
            for handle in self._dump.values():
                handle.close()
            self._dump = None

    def _accumulate_erle(self, mic: bytes, cleaned: bytes, played: bytes) -> None:
        """Log echo return loss enhancement while the speaker is actually active.

        This is the number that says whether the canceller is doing anything:
        0 dB means it is not.
        """
        if not any(played):
            return
        import numpy as np

        m = np.frombuffer(mic, dtype=np.int16).astype(np.float32)
        c = np.frombuffer(cleaned, dtype=np.int16).astype(np.float32)
        self._erle_mic += float(np.mean(m ** 2))
        self._erle_out += float(np.mean(c ** 2))
        self._erle_frames += 1
        now = time.monotonic()
        if self._erle_frames >= 100 and now - self._erle_log_at > 2.0:
            if self._erle_out > 0 and self._erle_mic > 0:
                import math

                erle = 10 * math.log10(self._erle_mic / self._erle_out)
                logger.info("AEC ERLE %.1f dB over %d frames", erle, self._erle_frames)
            self._erle_log_at = now
            self._erle_mic = self._erle_out = 0.0
            self._erle_frames = 0

    def _open_dump(self, dump_dir: str, rate: int) -> None:
        import wave

        os.makedirs(dump_dir, exist_ok=True)
        self._dump = {}
        for name in ("mic", "ref", "out"):
            handle = wave.open(os.path.join(dump_dir, f"aec_{name}.wav"), "wb")
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            self._dump[name] = handle
        logger.info("AEC dumping mic/ref/out wavs to %s", dump_dir)


class AecStream:
    """Mic stream wrapper that cancels echo out of every read.

    Wraps any object with the `sd.InputStream` read contract (also satisfied by
    ArecordStream): `read(frames) -> (int16 ndarray, overflowed)`.
    """

    def __init__(self, inner, canceller: EchoCanceller, tail_s: float, np):
        self._inner = inner
        self._canceller = canceller
        self._tail_s = tail_s
        self._np = np
        self._bypassed = True

    def __enter__(self):
        self._inner = self._inner.__enter__()
        self._canceller.reset()
        return self

    def __exit__(self, *args):
        return self._inner.__exit__(*args)

    def read(self, frames):
        data, overflowed = self._inner.read(frames)
        if overflowed:
            return data, overflowed
        # Skip the APM entirely when nothing has played recently — echo only
        # exists near playback, and this is a 24/7 loop on an 8-core A55.
        if self._canceller._reference.idle_for() > self._tail_s:
            if not self._bypassed:
                self._canceller.reset()
                self._bypassed = True
            return data, overflowed
        self._bypassed = False
        try:
            cleaned = self._canceller.process(data.tobytes())
        except Exception as e:
            logger.warning("AEC process failed, passing mic through: %s", e)
            return data, overflowed
        return (
            self._np.frombuffer(cleaned, dtype=self._np.int16).reshape(frames, -1),
            overflowed,
        )

    def __getattr__(self, name):
        return getattr(self._inner, name)


def configure(rate: int) -> bool:
    """Build the canceller for a mic sample rate. Safe to call repeatedly.

    Returns False when AEC is off, the rate is unsupported, or the native
    binding is missing — callers then use the raw mic.
    """
    global _canceller, _reference, _unavailable_logged

    from hal.drivers.voice._internal import config as voice_cfg

    if not voice_cfg.AEC_ENABLED:
        return False
    if rate not in SUPPORTED_RATES:
        logger.warning("AEC disabled: %dHz not supported by the APM %s", rate, SUPPORTED_RATES)
        return False
    with _lock:
        if _canceller is not None and _canceller._rate == rate:
            return True
        if _canceller is not None:
            _canceller.close()
            _canceller = None
        try:
            _reference = EchoReference(rate)
            _canceller = EchoCanceller(
                rate,
                _reference,
                voice_cfg.AEC_DELAY_MS,
                voice_cfg.AEC_NOISE_SUPPRESSION,
                voice_cfg.AEC_DUMP_DIR,
            )
        except Exception as e:
            _reference = None
            _canceller = None
            if not _unavailable_logged:
                _unavailable_logged = True
                logger.warning(
                    "AEC unavailable (%s) — mic runs uncancelled. Install with: "
                    "uv pip install aec-audio-processing",
                    e,
                )
            return False
    return True


def wrap_mic(mic_ctx, rate: int, np):
    """Return `mic_ctx` wrapped with echo cancellation, or unchanged when off."""
    if not configure(rate):
        return mic_ctx
    from hal.drivers.voice._internal import config as voice_cfg

    return AecStream(mic_ctx, _canceller, voice_cfg.AEC_TAIL_S, np)


def reference_write(samples, rate: int) -> None:
    """Record float32 audio on its way to the speaker. Never raises.

    Called from the TTS output stream write, i.e. at playback rate — which is
    the timing the mic sees. Tapping at synthesis instead would be wrong: TTS
    renders a sentence far faster than real time.
    """
    ref = _reference
    if ref is None or _canceller is None:
        return
    try:
        import numpy as np

        mono = np.asarray(samples, dtype=np.float32).reshape(-1)
        dst = _canceller._rate
        if rate != dst:
            import scipy.signal

            g = gcd(dst, rate)
            mono = scipy.signal.resample_poly(mono, dst // g, rate // g)
        pcm16 = (np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16)
        ref.write(pcm16.tobytes())
    except Exception as e:
        logger.debug("AEC reference write skipped: %s", e)


def reset() -> None:
    """Drop canceller state — call after an audio route change."""
    if _canceller is not None:
        _canceller.reset()
