"""
TTS Service — converts text to speech and plays through speaker.

Supports pluggable backends (OpenAI, ElevenLabs) via tts_backend.py.
Streams PCM chunks directly to the audio device — no buffering the entire response.
Runs synthesis in a background thread to avoid blocking FastAPI.
"""

import hashlib
import logging
import math
import os
import queue
import re
import threading
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np

from hal.drivers.voice.tts.backend import TTSBackend, TTS_SAMPLE_RATE, create_backend

# WAV cache for fixed-text TTS (fillers, intent confirms). Key includes
# provider/voice/model/speed/text so config changes self-invalidate.
_TTS_CACHE_DIR = Path(
    os.environ.get("HAL_TTS_CACHE_DIR", "/var/lib/hal/tts_cache")
)
# Per-key render lock map -- prevents two concurrent prerenders for same text
# from racing on the same WAV file.
_render_locks: dict[str, threading.Lock] = {}
_render_locks_mu = threading.Lock()


def _render_lock_for(key: str) -> threading.Lock:
    with _render_locks_mu:
        lock = _render_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _render_locks[key] = lock
        return lock

logger = logging.getLogger("hal.voice.tts")

DEFAULT_VOICE = "alloy"
DEFAULT_MODEL = "tts-1"

TTS_CHANNELS = 1


class _PendingSpeech:
    """One queued speak_queue() request waiting to play after the current TTS
    ends. Pre-synthesized in a background thread via a producer queue so the
    drain can play each frame the moment it arrives (no buffer-everything-
    first delay) and pre-synth time is hidden behind the previous speech's
    playback. Mirrors the tail_producer/tail_q pattern in _speak_sync."""

    __slots__ = ("text", "interruptible", "frame_queue", "failed")

    def __init__(self, text: str, interruptible: bool):
        self.text = text
        self.interruptible = interruptible
        # Producer (pre-synth thread) appends numpy frames as they arrive
        # from the backend; consumer (_drain_pending_queue) writes them to
        # the ALSA stream. None sentinel = producer is done.
        self.frame_queue: "queue.Queue" = queue.Queue(maxsize=128)
        self.failed = False


class TTSService:
    """Text-to-speech with pluggable backend + sounddevice streaming playback."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        sound_device_module=None,
        numpy_module=None,
        output_device: Optional[int] = None,
        voice: str = DEFAULT_VOICE,
        model: str = DEFAULT_MODEL,
        max_retries: int = 3,
        speed: float = 1.0,
        instructions: Optional[str] = None,
        on_speak_start=None,
        on_speak_end=None,
        provider: str = "openai",
    ):
        self._sd = sound_device_module
        self._np = numpy_module
        self._output_device = output_device
        self._provider = provider
        self._voice = voice
        self._model = model
        self._speed = max(0.25, min(4.0, speed))
        self._instructions = instructions
        self._lock = threading.Lock()
        self._speaking = False
        self._interruptible = False
        self._max_retries = max_retries
        self._stop_event = threading.Event()

        # speak_queue() drops items here when the lock is held by another
        # speech. Background threads pre-synthesize each item's PCM frames
        # while the current speech plays; _drain_pending_queue() writes those
        # frames to the same open ALSA stream so playback continues with no
        # synth-TTFB gap between sentences. stop() clears the queue.
        self._pending_queue_lock = threading.Lock()
        self._pending_queue: list = []  # list[_PendingSpeech]

        # Optional callbacks for LED speaking effect.
        # on_speak_start(): called when TTS playback begins (before audio streams).
        # on_speak_end():   called when TTS playback finishes or is interrupted.
        self._on_speak_start = on_speak_start
        self._on_speak_end = on_speak_end

        # Echo cancellation: store last spoken text for transcript self-filtering
        self._last_spoken_text: str = ""
        self._last_spoken_time: float = 0.0

        self._device_rate = None
        self._backend: Optional[TTSBackend] = None
        try:
            self._backend = create_backend(provider=provider, api_key=api_key, base_url=base_url)
            logger.info(
                "TTS ready (provider=%s, voice=%s, model=%s)",
                provider,
                self._voice,
                self._model,
            )
        except Exception:
            logger.exception("TTS backend init failed")

        # Probe device sample rate by actually opening a stream (check_output_settings
        # is unreliable on some ALSA devices like seeed-2mic wm8960, CD002-AUDIO)
        if self._sd:
            self._probe_device_rate()

        # Persistent OutputStream + silence keepalive — eliminates ~4s ALSA codec
        # warmup on every speak by keeping the stream open across speaks.
        # Silence writer prevents the codec from suspending during idle.
        self._stream = None
        self._stream_rate: Optional[int] = None
        self._stream_lock = threading.Lock()
        if self._sd and self._device_rate:
            try:
                self._ensure_stream(self._device_rate)
                threading.Thread(
                    target=self._silence_keepalive,
                    daemon=True,
                    name="tts-silence-keepalive",
                ).start()
            except Exception:
                logger.exception("Persistent stream init failed")

    def _ensure_stream(self, dst_rate: int):
        """Open persistent OutputStream or return existing one. Reopens if rate
        changed or previous stream was invalidated. Caller must hold _stream_lock
        OR be sure no other thread can race (init path)."""
        if (
            self._stream is not None
            and self._stream_rate == dst_rate
        ):
            return self._stream

        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
            self._stream_rate = None

        stream = self._sd.OutputStream(
            samplerate=dst_rate,
            channels=TTS_CHANNELS,
            dtype="float32",
            device=self._output_device,
        )
        stream.start()
        self._stream = stream
        self._stream_rate = dst_rate
        logger.info("Persistent OutputStream opened at %d Hz", dst_rate)
        return stream

    def _invalidate_stream(self):
        """Force the persistent stream to be reopened on next use (after a
        write failure, e.g. ALSA underrun or codec rejecting buffer)."""
        with self._stream_lock:
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
                self._stream_rate = None

    def release_stream(self):
        """Public: close the persistent stream so other ALSA consumers
        (music ffmpeg|aplay subprocess, /audio/play-tone, /audio/record)
        can grab the device exclusively. Stream is reopened lazily by the
        next speak() / speak_cached() call."""
        self._invalidate_stream()

    def _silence_keepalive(self):
        """Write 20ms of silence every 500ms when idle to keep the codec out of
        suspend. WM8960/Rockchip codecs power down PCM after ~1s idle, which
        forces a multi-second snd_pcm_prepare on the next write."""
        np = self._np
        while True:
            time.sleep(0.5)
            if self._speaking:
                continue
            try:
                with self._stream_lock:
                    if self._stream is None or self._stream_rate is None:
                        continue
                    if self._speaking:
                        continue
                    silence = np.zeros((self._stream_rate // 50, 1), dtype=np.float32)
                    self._stream.write(silence)
            except Exception as e:
                logger.debug("Silence keepalive write failed, invalidating: %s", e)
                # Don't call _invalidate_stream() under lock recursively.
                try:
                    if self._stream is not None:
                        self._stream.close()
                except Exception:
                    pass
                self._stream = None
                self._stream_rate = None

    def _probe_device_rate(self, force: bool = False):
        """Probe the output device to find a supported sample rate.

        In-memory cache only via self._device_rate -- probe runs once per
        TTSService lifetime, ~50ms total. force=True bypasses the cache
        (used by the playback retry path when the cached rate stops working).
        """
        if not force and self._device_rate:
            return

        dev_label = (
            self._output_device if self._output_device is not None else "default"
        )
        self._device_rate = None
        for rate in [44100, 48000, 16000, 32000, 24000, 22050, 8000]:
            try:
                # Write only ~5ms of silence -- enough to verify the rate opens
                # without forcing a multi-second snd_pcm_drain on close (OrangePi).
                probe_frames = max(1, int(rate * 0.005))
                with self._sd.OutputStream(
                    device=self._output_device,
                    samplerate=rate,
                    channels=TTS_CHANNELS,
                    dtype="float32",
                ) as stream:
                    _ = stream.write(np.zeros(probe_frames, dtype=np.float32))
                self._device_rate = rate
                logger.info("Output device [%s]: verified rate=%d Hz", dev_label, rate)
                break
            except Exception as e:
                logger.debug("Failed to play audio with rate=%d Hz due to e=%s", dev_label, e)

        if self._device_rate is None:
            logger.warning(
                "No supported sample rate found for output device [%s]", dev_label
            )

    @property
    def available(self) -> bool:
        return self._backend is not None and self._backend.available and self._sd is not None

    @property
    def speaking(self) -> bool:
        return self._speaking

    @property
    def last_spoken_text(self) -> str:
        """Last text sent to TTS (for echo cancellation transcript filtering)."""
        return self._last_spoken_text

    @property
    def last_spoken_time(self) -> float:
        """Timestamp when last TTS playback finished."""
        return self._last_spoken_time

    def stop(self):
        """Interrupt active TTS playback. No-op if not speaking.

        Also clears the speak_queue() pending list — pre-synth'd PCM is
        discarded so a stop during sentence-streaming actually silences
        everything the agent had queued ahead, not just the current sentence.
        """
        if self._speaking:
            logger.info("TTS stop requested — setting stop event")
            self._stop_event.set()
        with self._pending_queue_lock:
            cleared = len(self._pending_queue)
            self._pending_queue.clear()
        if cleared:
            logger.info("TTS stop cleared %d pending queued speech item(s)", cleared)

    @property
    def interruptible(self) -> bool:
        """Whether the current speech can be interrupted by a new speak() call."""
        return self._interruptible

    def speak(self, text: str, interruptible: bool = False) -> bool:
        """Synthesize and play text. Returns True if started, False if busy or unavailable.
        If interruptible=True, a subsequent speak() call can stop this one."""
        if not self.available:
            logger.warning("TTS not available")
            return False

        if not self._lock.acquire(blocking=False):
            # Busy — but if current speech is interruptible, stop it and retry
            if self._interruptible:
                logger.info("TTS interrupting interruptible speech for: %s", text[:50])
                self.stop()
                # Wait briefly for lock release
                if not self._lock.acquire(blocking=True, timeout=2.0):
                    logger.warning("TTS lock not released after stop, giving up: %s", text[:50])
                    return False
            else:
                logger.info("TTS busy, skipping: %s", text[:50])
                return False

        # Clear any leftover stop signal from a previous stop() call
        self._stop_event.clear()

        # Mark speaking IMMEDIATELY so VoiceService stops streaming to Deepgram
        # before TTS API call (which can take 3-5s)
        self._speaking = True
        self._interruptible = interruptible
        self._last_spoken_text = text

        thread = threading.Thread(
            target=self._speak_sync,
            args=(text,),
            daemon=True,
            name="tts-speak",
        )
        thread.start()
        return True

    def speak_queue(self, text: str, interruptible: bool = False) -> bool:
        """Speak `text`. If TTS is idle, plays immediately (same as speak()).
        If TTS is currently speaking, the text is appended to a pending queue
        and pre-synthesized in the background; once the current playback
        finishes, the pre-synth'd PCM frames stream to the same open ALSA
        stream with no synth-TTFB gap.

        Used by the SSE handler to dispatch sentence-streamed agent replies:
        the first sentence lands while idle (immediate play); subsequent
        sentences arrive while the first is still playing and are queued so
        the user hears one continuous reply with no per-sentence pause.

        Returns True if the request was accepted (playing or queued); False
        if TTS is unavailable.
        """
        if not self.available:
            logger.warning("TTS not available")
            return False

        if self._lock.acquire(blocking=False):
            # Idle — start a normal speech (same as speak()).
            self._stop_event.clear()
            self._speaking = True
            self._interruptible = interruptible
            self._last_spoken_text = text
            thread = threading.Thread(
                target=self._speak_sync,
                args=(text,),
                daemon=True,
                name="tts-speak-queue",
            )
            thread.start()
            return True

        # Busy — queue + kick off pre-synth so frames are ready when the
        # current speech ends. We don't try to interrupt even if the current
        # speech is interruptible; the whole point of speak_queue() is to
        # chain on top of the current speech, not replace it.
        item = _PendingSpeech(text=text, interruptible=interruptible)
        with self._pending_queue_lock:
            self._pending_queue.append(item)
        threading.Thread(
            target=self._pre_synth_pending,
            args=(item,),
            daemon=True,
            name="tts-pre-synth",
        ).start()
        logger.info(
            "TTS queued for pre-synth (busy, queue depth=%d): %s",
            len(self._pending_queue),
            text[:60],
        )
        return True

    def _pre_synth_pending(self, item: "_PendingSpeech") -> None:
        """Synthesize PCM for a queued item in a background thread. Streams
        frames into item.frame_queue as they arrive from the backend so the
        drain consumer can start playing on the first frame (~1.5s TTFB)
        without waiting for the entire batch to synthesize. A long batch
        (3-4 chunks, 300+ chars) takes 10-15s end-to-end; buffer-then-play
        would underrun the drain's timeout.

        Honors _stop_event so stop() during pre-synth aborts cleanly. Always
        writes the None sentinel at the end so the drain stops looking.
        """
        try:
            dst_rate = self._device_rate or TTS_SAMPLE_RATE
            chunks = self._split_text_into_growing_sentence_chunks(item.text)
            for chunk_text in chunks:
                if self._stop_event.is_set():
                    return
                for frame in self._iter_tts_samples(chunk_text, dst_rate, ttfb_tag="pre-synth"):
                    if self._stop_event.is_set():
                        return
                    item.frame_queue.put(frame)
        except Exception:
            logger.exception("Pre-synth failed for queued item")
            item.failed = True
        finally:
            # Sentinel — drain stops reading. Never block here even if the
            # consumer hasn't drained: queue is bounded but the producer is
            # done, so the put is allowed.
            try:
                item.frame_queue.put(None, timeout=1.0)
            except queue.Full:
                pass

    def _drain_pending_queue(self, stream) -> int:
        """Stream pre-synth'd frames from each pending queue item to the open
        ALSA stream as they arrive. Called from _speak_sync after the main
        playback's tail chunks drain but before the stream lock is released
        — so the queued frames continue on the same audio output and the
        user hears no inter-speech gap.

        First-frame wait is bounded (handles the case where pre-synth is
        slow or hung); intra-stream wait is longer to ride out backend
        slowdowns mid-batch without abandoning the speech.

        Returns total samples written.
        """
        total = 0
        while not self._stop_event.is_set():
            with self._pending_queue_lock:
                if not self._pending_queue:
                    break
                item = self._pending_queue.pop(0)
            try:
                first = item.frame_queue.get(timeout=15.0)
            except queue.Empty:
                logger.warning("Pre-synth no first frame within 15s, abandoning: %s", item.text[:60])
                continue
            if first is None:
                if item.failed:
                    logger.warning("Pre-synth failed for queued speech: %s", item.text[:60])
                else:
                    logger.warning("Pre-synth produced no frames, skipping: %s", item.text[:60])
                continue
            self._last_spoken_text = item.text
            logger.info("Playing pre-synth'd queued speech (streaming): %s", item.text[:80])
            stream.write(first)
            total += len(first)
            while not self._stop_event.is_set():
                try:
                    frame = item.frame_queue.get(timeout=30.0)
                except queue.Empty:
                    logger.warning("Pre-synth stalled (no frame within 30s), ending speech early: %s", item.text[:60])
                    break
                if frame is None:
                    break
                stream.write(frame)
                total += len(frame)
        return total

    def _resample(self, audio, src_rate: int, dst_rate: int):
        """Linear interpolation resample (no scipy needed)."""
        np = self._np
        if src_rate == dst_rate:
            return audio
        ratio = dst_rate / src_rate
        n_out = math.ceil(len(audio) * ratio)
        x_old = np.linspace(0, 1, len(audio))
        x_new = np.linspace(0, 1, n_out)
        return np.interp(x_new, x_old, audio).astype(np.float32)

    def _split_text_into_growing_sentence_chunks(
        self,
        text: str,
        base_chars: int = 60,
        growth_factor: float = 2.0,
        max_chunk_chars: int = 520,
        max_chunks: int = 12,
    ) -> list[str]:
        """Split text into sentence-aligned chunks with growing size.

        First chunk (c0) is small (base_chars=60) so ElevenLabs returns the
        first PCM byte sooner -- TTFB scales with text length. Tail chunks
        grow to max_chunk_chars to amortize HTTP overhead. Net: lower
        perceived latency on longer responses without inflating chunk count
        for very short texts (which fit in c0 anyway).
        """
        normalized = re.sub(r"\s+", " ", (text or "").strip())
        if not normalized:
            return []

        parts = re.findall(r"[^.!?;:]+[.!?;:]*", normalized)
        parts = [p.strip() for p in parts if p and p.strip()]
        if not parts:
            return [normalized]

        chunks: list[str] = []
        idx = 0
        chunk_i = 0
        while idx < len(parts) and len(chunks) < max_chunks:
            target = int(base_chars * (growth_factor ** chunk_i))
            target = min(max(target, base_chars), max_chunk_chars)
            current: list[str] = []
            while idx < len(parts):
                s = parts[idx]
                candidate = " ".join(current + [s]).strip() if current else s
                if current and len(candidate) > target:
                    break
                current.append(s)
                idx += 1
                if len(" ".join(current)) >= target:
                    break
            if current:
                chunks.append(" ".join(current).strip())
                chunk_i += 1
            else:
                chunks.append(parts[idx])
                idx += 1

        if idx < len(parts) and chunks:
            remainder = " ".join(parts[idx:]).strip()
            if remainder:
                chunks[-1] = f"{chunks[-1]} {remainder}".strip()
        return [c for c in chunks if c]

    def _iter_tts_samples(self, text: str, dst_rate: int, ttfb_tag: Optional[str] = None):
        """Yield float32 sample frames from the TTS backend's PCM stream."""
        np = self._np
        src_rate = self._backend.sample_rate
        remainder = b""
        first_audio_logged = False
        t0 = time.perf_counter()

        for chunk in self._backend.stream_pcm(
            text=text,
            voice=self._voice,
            model=self._model,
            speed=self._speed,
            instructions=self._instructions,
        ):
            if self._stop_event.is_set():
                return
            raw = remainder + chunk
            usable = len(raw) - (len(raw) % 2)
            remainder = raw[usable:]
            if usable == 0:
                continue
            samples = (
                np.frombuffer(raw[:usable], dtype=np.int16).astype(np.float32)
                / 32768.0
            )
            # Boost TTS volume (provider-specific: OpenAI 2.5x, ElevenLabs 1.5x)
            samples = np.clip(samples * self._backend.volume_boost, -1.0, 1.0)
            if dst_rate != src_rate:
                samples = self._resample(samples, src_rate, dst_rate)
            if ttfb_tag and not first_audio_logged:
                first_audio_logged = True
                logger.info(
                    "TTS %s first audio frame: %.0fms",
                    ttfb_tag,
                    (time.perf_counter() - t0) * 1000.0,
                )
            yield samples.reshape(-1, 1)

        if not self._stop_event.is_set() and len(remainder) >= 2:
            usable = len(remainder) - (len(remainder) % 2)
            samples = (
                np.frombuffer(remainder[:usable], dtype=np.int16).astype(np.float32)
                / 32768.0
            )
            if dst_rate != src_rate:
                samples = self._resample(samples, src_rate, dst_rate)
            yield samples.reshape(-1, 1)

    def _stream_chunk_with_retry(self, stream, text: str, dst_rate: int, idx: int, total: int, ttfb_tag: Optional[str] = None) -> int:
        """Stream one text chunk with retry; return written sample count."""
        total_samples = 0
        attempt = 0
        while attempt <= self._max_retries:
            try:
                logger.info(
                    "TTS chunk %d/%d: len=%d (attempt=%d, speed=%.2f)",
                    idx,
                    total,
                    len(text),
                    attempt + 1,
                    self._speed,
                )
                for frame in self._iter_tts_samples(text, dst_rate, ttfb_tag=ttfb_tag):
                    if self._stop_event.is_set():
                        return total_samples
                    # Fire on_speak_start on first audio frame — syncs LED effect
                    # with actual audio output, not with TTS API call
                    if not self._speak_start_fired and self._on_speak_start:
                        self._speak_start_fired = True
                        try:
                            self._on_speak_start()
                        except Exception:
                            logger.exception("on_speak_start callback failed")
                    stream.write(frame)
                    total_samples += len(frame)
                return total_samples
            except Exception as e:
                logger.exception(
                    "TTS chunk failed (chunk=%d/%d, attempt=%d/%d)",
                    idx,
                    total,
                    attempt + 1,
                    self._max_retries + 1,
                )
                # Server-side errors (404, 503) — no point retrying or probing device
                status = getattr(e, "status_code", None)
                if status in (404, 503):
                    logger.warning("TTS server error %s — skipping retries", status)
                    break
                if attempt < self._max_retries:
                    self._probe_device_rate(force=True)
                attempt += 1
        logger.error("TTS give up for chunk %d/%d: text='%s'", idx, total, text[:80])
        return total_samples

    def _head_producer(
        self,
        text: str,
        dst_rate: int,
        out_q: "queue.Queue[Optional[np.ndarray]]",
        idx_total: tuple[int, int],
    ) -> None:
        """Produce head chunk frames into a queue. Runs in parallel with the
        ALSA OutputStream open call so HTTP TTFB overlaps codec warmup."""
        idx, total = idx_total
        attempt = 0
        try:
            while attempt <= self._max_retries:
                try:
                    logger.info(
                        "TTS chunk %d/%d: len=%d (attempt=%d, speed=%.2f)",
                        idx, total, len(text), attempt + 1, self._speed,
                    )
                    for frame in self._iter_tts_samples(text, dst_rate, ttfb_tag="c0"):
                        if self._stop_event.is_set():
                            return
                        out_q.put(frame)
                    return
                except Exception as e:
                    logger.exception(
                        "TTS head chunk failed (attempt=%d/%d)",
                        attempt + 1, self._max_retries + 1,
                    )
                    status = getattr(e, "status_code", None)
                    if status in (404, 503):
                        return
                    attempt += 1
        finally:
            try:
                out_q.put_nowait(None)
            except Exception:
                pass

    def _tail_producer(
        self,
        tail_chunks: list[str],
        dst_rate: int,
        out_q: "queue.Queue[Optional[np.ndarray]]",
    ) -> None:
        """Produce tail frames sequentially into one shared queue."""
        total = len(tail_chunks) + 1
        try:
            for i, chunk_text in enumerate(tail_chunks, start=2):
                if self._stop_event.is_set():
                    break
                attempt = 0
                while attempt <= self._max_retries:
                    try:
                        logger.info("Tail producer start c%d/%d len=%d", i, total, len(chunk_text))
                        for frame in self._iter_tts_samples(chunk_text, dst_rate):
                            if self._stop_event.is_set():
                                return
                            out_q.put(frame)
                        logger.info("Tail producer done  c%d/%d", i, total)
                        break
                    except Exception as e:
                        logger.exception(
                            "Tail producer failed (chunk=%d/%d, attempt=%d/%d)",
                            i,
                            total,
                            attempt + 1,
                            self._max_retries + 1,
                        )
                        status = getattr(e, "status_code", None)
                        if status in (404, 503):
                            logger.warning("TTS server error %s — skipping retries", status)
                            return
                        if attempt < self._max_retries:
                            self._probe_device_rate()
                        attempt += 1
                    if attempt > self._max_retries:
                        break
        finally:
            try:
                out_q.put_nowait(None)
            except Exception:
                pass

    def _speak_sync(self, text: str):
        """Head chunk direct playback + parallel tail producer queue."""
        sd = self._sd
        dst_rate = self._device_rate or TTS_SAMPLE_RATE
        chunks = self._split_text_into_growing_sentence_chunks(text)
        for i, c in enumerate(chunks):
            preview = c[:140] + ("..." if len(c) > 140 else "")
            logger.info("[chunk-split] c%d/%d len=%d text='%s'", i, len(chunks) - 1, len(c), preview)

        if not chunks:
            self._speaking = False
            self._last_spoken_time = time.time()
            self._lock.release()
            return

        # _on_speak_start fires on first audio frame, not here — see _stream_chunk_with_retry
        self._speak_start_fired = False

        head_text = chunks[0]
        tail_chunks = chunks[1:]
        total_samples = 0

        # Use cached rate from __init__. Re-probe only as fallback when playback
        # actually fails (handled by the retry loop below). Pre-probing on every
        # speak() blocked ~5s on OrangePi due to ALSA snd_pcm_drain after the 1s
        # silence write — diagnosed 2026-05-05 from server.log.
        dst_rate = self._device_rate or TTS_SAMPLE_RATE

        # Start head HTTP fetch BEFORE opening the OutputStream so ElevenLabs TTFB
        # (~1.5s through proxy) overlaps with ALSA codec open (multi-second on cold
        # OrangePi). By the time `with sd.OutputStream(...)` returns, first frames
        # are usually already in the queue.
        head_total = len(chunks)
        head_q: "queue.Queue[Optional[np.ndarray]]" = queue.Queue(maxsize=256)
        head_thread = threading.Thread(
            target=self._head_producer,
            args=(head_text, dst_rate, head_q, (1, head_total)),
            daemon=True,
            name="tts-head-producer",
        )
        head_thread.start()

        for _play_attempt in range(2):
            try:
                # Acquire the stream lock for the entire playback so the silence
                # keepalive thread doesn't interleave zeros with TTS frames.
                with self._stream_lock:
                    stream = self._ensure_stream(dst_rate)
                    # Tail producer kicks off the moment stream is ready so its HTTP
                    # TTFB overlaps with head playback.
                    tail_q: Optional["queue.Queue[Optional[np.ndarray]]"] = None
                    tail_thread: Optional[threading.Thread] = None
                    if tail_chunks:
                        tail_q = queue.Queue(maxsize=128)
                        tail_thread = threading.Thread(
                            target=self._tail_producer,
                            args=(tail_chunks, dst_rate, tail_q),
                            daemon=True,
                            name="tts-tail-producer",
                        )
                        tail_thread.start()

                    # Drain head queue. First-frame latency = max(stream open, HTTP TTFB)
                    # on cold start; ~HTTP TTFB only on warm stream (subsequent speaks).
                    while not self._stop_event.is_set():
                        try:
                            item = head_q.get(timeout=2.0)
                        except queue.Empty:
                            if not head_thread.is_alive():
                                break
                            continue
                        if item is None:
                            break
                        if not self._speak_start_fired and self._on_speak_start:
                            self._speak_start_fired = True
                            try:
                                self._on_speak_start()
                            except Exception:
                                logger.exception("on_speak_start callback failed")
                        stream.write(item)
                        total_samples += len(item)

                    # Drain tail queue.
                    if tail_q is not None and tail_thread is not None:
                        while not self._stop_event.is_set():
                            if (not tail_thread.is_alive()) and tail_q.empty():
                                break
                            try:
                                item = tail_q.get(timeout=0.3)
                            except queue.Empty:
                                continue
                            if item is None:
                                break
                            stream.write(item)
                            total_samples += len(item)
                    # speak_queue() may have parked pre-synth'd PCM behind us
                    # while this speech played. Drain that queue on the same
                    # open ALSA stream so the queued speech continues with no
                    # synth-TTFB gap (the agent's sentence-streamed batch).
                    total_samples += self._drain_pending_queue(stream)
                break  # playback succeeded, exit retry loop
            except Exception:
                logger.exception("TTS playback setup failed")
                # Stream is suspect -- close and reopen on retry.
                self._invalidate_stream()
                if _play_attempt == 0:
                    logger.warning("Re-probing output device rate and retrying...")
                    self._probe_device_rate(force=True)
                    dst_rate = self._device_rate or TTS_SAMPLE_RATE
                    # Old head producer is at the stale rate -- orphan it and
                    # restart at the new rate. Daemon thread will exit on its own.
                    head_q = queue.Queue(maxsize=256)
                    head_thread = threading.Thread(
                        target=self._head_producer,
                        args=(head_text, dst_rate, head_q, (1, head_total)),
                        daemon=True,
                        name="tts-head-producer-retry",
                    )
                    head_thread.start()

        logger.info(
            "TTS playback complete (%d samples @ %d Hz, chunks=%d)",
            total_samples,
            dst_rate,
            len(chunks),
        )

        self._speaking = False
        self._last_spoken_time = time.time()

        # Notify LED speaking effect — stop wave and restore previous LED state
        if self._on_speak_end:
            try:
                self._on_speak_end()
            except Exception:
                logger.exception("on_speak_end callback failed")

        self._lock.release()

    # ──────────────────────────────────────────────────────────────────────
    # WAV cache for fixed-text speeches (fillers, intent confirms).
    # Cache hit -> ~50ms playback (no ElevenLabs roundtrip).
    # Cache miss -> render full WAV, save, then play. Subsequent calls hit.
    # ──────────────────────────────────────────────────────────────────────

    def _tts_cache_key(self, text: str) -> str:
        h = hashlib.sha1()
        h.update(self._provider.encode("utf-8"))
        h.update(b"\x00")
        h.update((self._voice or "").encode("utf-8"))
        h.update(b"\x00")
        h.update((self._model or "").encode("utf-8"))
        h.update(b"\x00")
        h.update(f"{self._speed:.2f}".encode("ascii"))
        h.update(b"\x00")
        h.update(text.encode("utf-8"))
        return h.hexdigest()

    def _tts_cache_path(self, text: str) -> Path:
        return _TTS_CACHE_DIR / f"{self._tts_cache_key(text)}.wav"

    def speak_cached(self, text: str, interruptible: bool = False, prerender: bool = False) -> bool:
        """Cache-aware speak. On hit -> ~50ms playback. On miss -> render+save
        then play. prerender=True skips playback (warmup-only)."""
        if not self.available:
            logger.warning("TTS not available (cached path)")
            return False

        cache_path = self._tts_cache_path(text)
        key = cache_path.name

        # Prerender: synchronous render+save, no playback. Caller (boot script)
        # typically iterates over a fixed phrase list.
        if prerender:
            with _render_lock_for(key):
                if cache_path.exists():
                    return True
                try:
                    self._render_and_save_wav(text, cache_path)
                    return True
                except Exception:
                    logger.exception("Prerender failed for %r", text[:50])
                    return False

        # Playback path: mirror speak() lock semantics.
        if not self._lock.acquire(blocking=False):
            if self._interruptible:
                logger.info("TTS interrupting (cached) for: %s", text[:50])
                self.stop()
                if not self._lock.acquire(blocking=True, timeout=2.0):
                    logger.warning("TTS lock not released after stop (cached): %s", text[:50])
                    return False
            else:
                logger.info("TTS busy, skipping cached: %s", text[:50])
                return False

        self._stop_event.clear()
        self._speaking = True
        self._interruptible = interruptible
        self._last_spoken_text = text

        threading.Thread(
            target=self._cached_play_thread,
            args=(text, cache_path),
            daemon=True,
            name="tts-cached-speak",
        ).start()
        return True

    def _cached_play_thread(self, text: str, cache_path: Path) -> None:
        """Render-on-miss + play. Runs with self._lock + self._speaking already
        set by speak_cached(). Releases them in the finally block."""
        cache_hit = cache_path.exists()
        try:
            if not cache_hit:
                with _render_lock_for(cache_path.name):
                    if not cache_path.exists():
                        self._render_and_save_wav(text, cache_path)
            self._play_wav_inline(cache_path, hit=cache_hit)
        except Exception:
            logger.exception("Cached speak thread failed")
        finally:
            self._speaking = False
            self._last_spoken_time = time.time()
            if self._on_speak_end:
                try:
                    self._on_speak_end()
                except Exception:
                    logger.exception("on_speak_end (cached) failed")
            try:
                self._lock.release()
            except Exception:
                pass

    def _play_wav_inline(self, path: Path, hit: bool = True) -> None:
        """Load WAV -> resample -> write to persistent stream. Acquires
        self._stream_lock for the playback duration."""
        t0 = time.perf_counter()
        with wave.open(str(path), "rb") as wav:
            src_rate = wav.getframerate()
            raw = wav.readframes(wav.getnframes())

        np = self._np
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if self._backend is not None:
            samples = np.clip(samples * self._backend.volume_boost, -1.0, 1.0)

        dst_rate = self._device_rate or TTS_SAMPLE_RATE
        if src_rate != dst_rate:
            samples = self._resample(samples, src_rate, dst_rate)
        samples = samples.reshape(-1, 1)

        if self._on_speak_start:
            try:
                self._on_speak_start()
            except Exception:
                logger.exception("on_speak_start (cached) failed")

        with self._stream_lock:
            stream = self._ensure_stream(dst_rate)
            # Write in 10ms blocks so stop() can cut in promptly.
            block = max(1, dst_rate // 100)
            for i in range(0, len(samples), block):
                if self._stop_event.is_set():
                    break
                stream.write(samples[i : i + block])

        logger.info(
            "TTS cached %s: %d samples @ %d Hz, took %.0fms (path=%s)",
            "HIT" if hit else "MISS-played",
            len(samples), dst_rate, (time.perf_counter() - t0) * 1000.0, path.name,
        )

    def _render_and_save_wav(self, text: str, cache_path: Path) -> None:
        """Pull all PCM from backend and write WAV atomically. Synchronous."""
        if self._backend is None:
            raise RuntimeError("TTS backend not initialized")
        t0 = time.perf_counter()
        pcm = bytearray()
        src_rate = self._backend.sample_rate
        for chunk in self._backend.stream_pcm(
            text=text,
            voice=self._voice,
            model=self._model,
            speed=self._speed,
            instructions=self._instructions,
        ):
            pcm.extend(chunk)

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_suffix(".wav.tmp")
        with wave.open(str(tmp_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(src_rate)
            wav.writeframes(bytes(pcm))
        tmp_path.replace(cache_path)
        logger.info(
            "TTS rendered to cache: %s (%d bytes, rate=%d, took %.0fms)",
            cache_path.name, len(pcm), src_rate,
            (time.perf_counter() - t0) * 1000.0,
        )
