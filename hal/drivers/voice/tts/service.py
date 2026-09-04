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

from hal.drivers.voice import aec
from hal.drivers.voice.tts.backend import (
    TTSBackend,
    TTS_SAMPLE_RATE,
    TTSRateLimitError,
    create_backend,
)

# Minimum gap between spoken rate-limit notices. While the provider stays
# rate-limited, every turn would otherwise replay the notice — debounce so the
# user hears it once, then silence until the window elapses.
_RATE_LIMIT_ANNOUNCE_INTERVAL_S = float(
    os.environ.get("HAL_TTS_RATE_LIMIT_NOTICE_INTERVAL_S", "300")
)

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
# A single chunk write normally completes in well under a second. A write
# blocked longer means the sink stopped pulling audio (classic case: a BT
# headset going idle/out of range mid-utterance — PortAudio then blocks
# forever, the stop event is never re-checked and `speaking` wedges True,
# gating the mic). The watchdog aborts the stream to unblock it.
TTS_WRITE_STALL_S = 8.0

# Slice size for the paced write below. Every slice costs one blocking
# PortAudio write plus one echo-reference write, both in Python holding the
# GIL. At 10ms a single reply is ~1600 round trips, and on lamp-0c89 — where
# vision keeps the interpreter's main thread pinned at ~100% — enough of them
# lost the GIL mid-utterance to be audible as stutter. 40ms cuts that 4x and
# still paces the reference far finer than AEC_REF_MS (500ms) of buffer.
TTS_REF_SLICE_S = 0.040


class _WatchedStream:
    """Thin wrapper over sounddevice.OutputStream that stamps when a blocking
    write() starts, so the stall watchdog can spot one wedged on a sink that
    stopped pulling audio and abort the stream from outside — the write then
    raises and the write site's normal error handling recovers."""

    def __init__(self, stream, owner):
        self._stream = stream
        self._owner = owner

    def write(self, data):
        self._owner._write_started_ts = time.monotonic()
        # Echo reference, paced to PLAYBACK, not to synthesis.
        #
        # Covers synthesized speech, the queue drain and realtime native audio,
        # since all three reach the device through this one stream.
        #
        # The reference goes in AFTER each slice has been handed to the device,
        # one slice at a time, because `self._stream.write` blocks until the sink
        # accepts the audio — so this loop advances at roughly the rate the
        # speaker plays. Publishing the whole `data` up front instead (what this
        # did until 26/08/2026) dumped a caller-sized block into a FIFO that only
        # holds REF_MS of audio: TTS synthesises far faster than realtime, so the
        # buffer overflowed and its overflow policy drops the OLDEST bytes —
        # exactly the audio the microphone was about to hear. What remained was
        # the reply's future, misaligned with the mic, and then nothing at all.
        # Measured on lamp-0c89 that left 59% of echo-bearing mic frames with no
        # reference, and the frames that had one cancelled just 5.4 dB. It also
        # explains why raising REF_MS made things WORSE (3.6 dB at 1500 vs 23.2
        # at 500): a bigger buffer lets the reference run further ahead of the
        # speaker, so the misalignment grows instead of the coverage.
        rate = self._owner._stream_rate
        # Slice the caller's own object so an ndarray stays an ndarray (its
        # dtype is what tells the device how to read the samples); len() is
        # frames for an ndarray and bytes for a buffer, hence the two steps.
        per_slice = max(1, int(rate * TTS_REF_SLICE_S))
        step = per_slice * 2 if isinstance(data, (bytes, bytearray, memoryview)) else per_slice
        try:
            total = len(data)
        except TypeError:
            total = 0
        if total == 0:
            try:
                return self._stream.write(data)
            finally:
                self._owner._write_started_ts = None
        try:
            result = None
            for off in range(0, total, step):
                chunk = data[off:off + step]
                try:
                    result = self._stream.write(chunk)
                except self._owner._sd.PortAudioError:
                    # Someone tore this stream down while we were mid-write:
                    # the stall watchdog aborting it, stop()/barge-in, or
                    # release_stream() handing the device to aplay. The owner
                    # has already dropped or replaced us, so the remaining
                    # slices have nowhere to go. Return quietly — raising here
                    # reaches the caller's generic handler, which reopens the
                    # device and re-speaks the whole utterance, replaying
                    # speech the user just cancelled. A stream that is still
                    # the owner's own is a genuine device failure: re-raise.
                    if self._owner._stream is self:
                        raise
                    logger.info(
                        "TTS write stopped mid-utterance at %d/%d — stream was "
                        "torn down by another thread; dropping the remainder",
                        off, total,
                    )
                    return result
                # Re-stamp per slice: the stall watchdog times ONE blocking
                # write, and a long block is now many short ones.
                self._owner._write_started_ts = time.monotonic()
                aec.reference_write(chunk, rate)
            return result
        finally:
            self._owner._write_started_ts = None

    def write_untapped(self, data):
        """Write without recording an echo reference.

        For audio that is not playback: the idle keepalive below pushes silence
        purely to stop the codec suspending. Tapping it kept EchoReference
        permanently non-idle, which defeated AecStream's bypass and left the APM
        gating the microphone between every word.
        """
        self._owner._write_started_ts = time.monotonic()
        try:
            return self._stream.write(data)
        finally:
            self._owner._write_started_ts = None

    def __getattr__(self, name):
        return getattr(self._stream, name)


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
        # Queues the active _speak_sync is draining, so stop() can wake a
        # drain loop parked in queue.get() instead of leaving it to time out.
        self._drain_queues: list = []
        self._drain_queues_lock = threading.Lock()
        self._max_retries = max_retries
        self._stop_event = threading.Event()

        # Set by the synth producers when the backend raises TTSRateLimitError so
        # _speak_sync can announce it (prerendered notice) after playback ends.
        # _last_rate_limit_announce debounces the notice across back-to-back
        # rate-limited turns (see _announce_rate_limit).
        self._rate_limit_hit = False
        self._last_rate_limit_announce = 0.0

        # speak_queue() drops items here when the lock is held by another
        # speech. Background threads pre-synthesize each item's PCM frames
        # while the current speech plays; _drain_pending_queue() writes those
        # frames to the same open ALSA stream so playback continues with no
        # synth-TTFB gap between sentences. stop() clears the queue.
        self._pending_queue_lock = threading.Lock()
        self._pending_queue: list = []  # list[_PendingSpeech]
        # A queue request may arrive out of order because the Go handler posts
        # each streamed segment in its own goroutine. Keep a turn sequence at
        # the HAL boundary so an old request cannot re-enter after a newer turn
        # has already taken the speaker. The request lock covers comparison,
        # preemption, and enqueue as one transaction.
        self._queue_request_lock = threading.Lock()
        self._latest_queue_turn_id = ""
        self._latest_queue_turn_seq = 0

        # Optional callbacks for LED speaking effect.
        # on_speak_start(): called when TTS playback begins (before audio streams).
        # on_speak_end():   called when TTS playback finishes or is interrupted.
        self._on_speak_start = on_speak_start
        self._on_speak_end = on_speak_end

        # on_unspoken_reply(text): an agent reply this service accepted and then
        # dropped without playing it — today, a superseded turn arriving after a
        # newer one already owns the queue. Injected by VoiceService (same way
        # as _on_speak_end) to feed the realtime agent, which otherwise learns
        # what the main agent replied only through playback. The drop is
        # acknowledged as success to os-server, so this is the ONLY place that
        # knows the text existed and was never heard.
        self._on_unspoken_reply = None

        # Echo cancellation: store last spoken text for transcript self-filtering
        self._last_spoken_text: str = ""
        self._last_spoken_time: float = 0.0

        # Native realtime playback (the model's own voice straight to the speaker):
        # _native_mode True from native_play_begin() through on_speak_end so the
        # realtime [TTS HISTORY] feedback hook can skip re-feeding (the model
        # generated this audio, it already knows). _native_src_rate = the model's
        # output rate, resampled to the device stream rate per frame.
        self._native_mode: bool = False
        self._native_src_rate: int = 0

        # Whether the CURRENT speech should be fed back to the realtime voice
        # agent as [TTS HISTORY] (via the on_speak_end hook in VoiceService).
        # Set per-speak: only the agentic runtime's reply passes True. Hardcoded
        # TTS (fillers, ambient mumble, system notices) leaves it False so those
        # never reach the realtime model. Default False = safe (no feedback).
        self._realtime_feedback: bool = False

        self._device_rate = None
        # Verified sample rate per output device (keyed by device NAME). Route
        # swaps re-probe the device; without this cache each swap burns ~5s
        # walking the rate list. The playback retry path bypasses + clears it.
        # MUST init before the first _probe_device_rate() call below.
        self._rate_cache: dict = {}
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
        self._write_started_ts: Optional[float] = None
        # Pre-rendered gesture-ack ping, cached per stream rate (see
        # play_ack_chime). Generated in-memory — no binary asset to ship.
        self._ack_chime_cache = None
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
        threading.Thread(
            target=self._write_watchdog, daemon=True, name="tts-write-watchdog"
        ).start()

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

        # Guarded open: refuses (RuntimeError) while a PortAudio re-init is in
        # flight — a stream that comes alive right before Pa_Terminate() aborts
        # the whole process. One failed write is recoverable; SIGABRT is not.
        from hal.drivers.audio_route import stream_open_guard

        with stream_open_guard():
            stream = self._sd.OutputStream(
                samplerate=dst_rate,
                channels=TTS_CHANNELS,
                dtype="float32",
                device=self._output_device,
            )
            stream.start()
        self._stream = _WatchedStream(stream, self)
        self._stream_rate = dst_rate
        logger.info("Persistent OutputStream opened at %d Hz", dst_rate)
        return self._stream

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
                    # Untapped: this is not playback, and recording it as an
                    # echo reference would keep the canceller permanently awake.
                    writer = getattr(self._stream, "write_untapped", None)
                    if writer is not None:
                        writer(silence)
                    else:
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

    def _write_watchdog(self):
        """Break writes wedged on a sink that stopped pulling audio.

        A blocking write past TTS_WRITE_STALL_S means the sink died mid-stream
        (BT headset idled/went out of range while still "connected", USB DAC
        unplugged): the stop event is only checked between writes, so without
        this the `speaking` flag wedges True and gates the mic forever.
        abort() forces the blocked write to raise; the write site's error
        handling then invalidates the stream and clears state. Two stalls in
        short order mean the route itself is dead — fall back to the built-in
        speaker so the voice pipeline stays usable."""
        last_fire = 0.0
        consecutive = 0
        while True:
            time.sleep(1.0)
            ts = self._write_started_ts
            if ts is None or (time.monotonic() - ts) < TTS_WRITE_STALL_S:
                continue
            self._write_started_ts = None
            now = time.monotonic()
            if now - last_fire > 120.0:
                consecutive = 0
            consecutive += 1
            last_fire = now
            logger.error(
                "TTS write stalled >%.0fs — sink stopped pulling audio; aborting stream (stall #%d)",
                TTS_WRITE_STALL_S, consecutive,
            )
            try:
                stream = self._stream
                if stream is not None:
                    # No _stream_lock here: the wedged writer holds it, and
                    # Pa_AbortStream is safe to call from another thread.
                    stream.abort()
            except Exception:
                logger.exception("Watchdog stream abort failed")
            if consecutive >= 2:
                consecutive = 0
                threading.Thread(
                    target=self._fallback_to_builtin, daemon=True, name="tts-bt-fallback"
                ).start()

    def _fallback_to_builtin(self):
        """Repeated stalled writes on a Bluetooth route: the headset is gone in
        practice even if BlueZ still shows it connected (e.g. AirPods parked in
        their case — in-ear detection keeps the link but stops the audio).
        Re-route to the built-in speaker/mic and clear the persisted preference
        so a HAL restart doesn't walk straight back into the dead route."""
        try:
            from hal.drivers import audio_route
            if not audio_route.bt_active():
                return
            logger.warning(
                "Repeated TTS stalls on BT route — falling back to built-in speaker"
            )
            from hal.drivers.bluetooth_manager import BluetoothManager
            with audio_route.route_op_lock:
                audio_route.route_to_builtin()
                BluetoothManager().set_active_mac(None)
        except Exception:
            logger.exception("BT→builtin fallback failed")

    def _device_key(self) -> str:
        """Stable cache key for the current output device — its PortAudio name
        when resolvable (indices shift after PortAudio re-inits), else the
        index."""
        idx = self._output_device
        try:
            if idx is not None and self._sd is not None:
                return str(self._sd.query_devices(idx)["name"])
        except Exception:
            pass
        return str(idx)

    def _probe_device_rate(self, force: bool = False, use_cache: bool = True):
        """Probe the output device to find a supported sample rate.

        force=True bypasses the self._device_rate short-circuit (route swaps
        change the device; retries suspect the rate). use_cache=False also
        ignores + clears the per-device rate cache — for the playback retry
        path, where the previously verified rate stopped working.
        """
        if not force and self._device_rate:
            return

        dev_label = (
            self._output_device if self._output_device is not None else "default"
        )
        key = self._device_key()
        if use_cache:
            cached = self._rate_cache.get(key)
            if cached:
                self._device_rate = cached
                logger.info(
                    "Output device [%s]: rate=%d Hz (cached for '%s')",
                    dev_label, cached, key,
                )
                return
        else:
            self._rate_cache.pop(key, None)
        self._device_rate = None
        for rate in [44100, 48000, 16000, 32000, 24000, 22050, 8000]:
            try:
                # Write only ~5ms of silence -- enough to verify the rate opens
                # without forcing a multi-second snd_pcm_drain on close (OrangePi).
                # Guarded like _ensure_stream: a probe stream alive during a
                # PortAudio re-init aborts the process just the same.
                from hal.drivers.audio_route import stream_open_guard

                probe_frames = max(1, int(rate * 0.005))
                with stream_open_guard(), self._sd.OutputStream(
                    device=self._output_device,
                    samplerate=rate,
                    channels=TTS_CHANNELS,
                    dtype="float32",
                ) as stream:
                    _ = stream.write(np.zeros(probe_frames, dtype=np.float32))
                self._device_rate = rate
                self._rate_cache[key] = rate
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
    def native_mode(self) -> bool:
        """True while playing the realtime model's own audio (native voice). The
        realtime [TTS HISTORY] feedback hook checks this to avoid re-feeding the
        model its own words."""
        return self._native_mode

    @property
    def realtime_feedback(self) -> bool:
        """Whether the current/last speech opted into realtime [TTS HISTORY]
        feedback. Only the agentic runtime's reply sets this; hardcoded TTS
        (fillers, mumble, system notices) leaves it False so the feedback hook
        skips them."""
        return self._realtime_feedback

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
            # Setting the event is not enough on its own: both drain loops
            # only re-check it between queue.get() calls, and the head queue's
            # get blocks for up to 2s. A newer turn that preempts this one
            # waits 2.0s for the lock (see speak_queue), so the winding-down
            # worker routinely lost that race — measured 4s to release on
            # lamp-0c89, which 503'd the new turn AFTER the old one had
            # already been cut to 85ms of audio: both turns silent. Feeding
            # each queue the same None sentinel the loops already treat as
            # end-of-stream wakes them immediately.
            self._wake_drain_queues()
        with self._pending_queue_lock:
            cleared = len(self._pending_queue)
            self._pending_queue.clear()
        if cleared:
            logger.info("TTS stop cleared %d pending queued speech item(s)", cleared)

    def _report_unspoken_reply(self, text: str, realtime_feedback: bool) -> None:
        """Hand a dropped agent reply to the unspoken-reply hook.

        Gated on realtime_feedback for the same reason the playback feed is:
        only the agentic runtime's own reply may enter the realtime model's
        context. A dropped filler or system notice must not.
        """
        if not realtime_feedback or not text or self._on_unspoken_reply is None:
            return
        try:
            self._on_unspoken_reply(text)
        except Exception:
            logger.exception("on_unspoken_reply callback failed")

    def _register_drain_queue(self, q) -> None:
        """Expose a queue the active playback is draining, so stop() can wake it."""
        with self._drain_queues_lock:
            self._drain_queues.append(q)

    def _forget_drain_queues(self) -> None:
        """Drop the registrations once playback is done with them."""
        with self._drain_queues_lock:
            self._drain_queues.clear()

    def _wake_drain_queues(self) -> None:
        """Push the end-of-stream sentinel into every registered queue.

        A full queue raises Full, which is fine to swallow: a full queue is by
        definition not one anybody is blocked on, so the loop will notice the
        stop event on its very next iteration anyway.
        """
        with self._drain_queues_lock:
            queues = list(self._drain_queues)
        for q in queues:
            try:
                q.put_nowait(None)
            except Exception:
                pass

    @property
    def interruptible(self) -> bool:
        """Whether the current speech can be interrupted by a new speak() call."""
        return self._interruptible

    @staticmethod
    def _speaker_muted() -> bool:
        """True when the device speaker is muted. Single mute gate for ALL TTS
        playback so no caller can bypass it (HTTP routes, realtime agent, etc.).
        Lazy import avoids an app_state import cycle (app_state holds the
        TTSService instance)."""
        try:
            from hal import app_state

            return app_state._speaker_muted
        except Exception:
            return False

    def speak(self, text: str, interruptible: bool = False, realtime_feedback: bool = False) -> bool:
        """Synthesize and play text. Returns True if started, False if busy or unavailable.
        If interruptible=True, a subsequent speak() call can stop this one.
        realtime_feedback=True feeds this text to the realtime agent on completion
        (agent replies only — see _realtime_feedback)."""
        if not self.available:
            logger.warning("TTS not available")
            return False

        if self._speaker_muted():
            logger.info("TTS suppressed -- speaker muted: %s", text[:50])
            return False

        # Cache-first: an exact-text WAV in the prerender cache plays with NO
        # API call. This is what keeps OS notices (rate-limit, LLM-limit)
        # audible while the TTS provider itself is rate-limited. Dynamic
        # agent replies never match — the cache only ever holds warm-listed
        # fixed phrases. speak_cached mirrors this method's lock semantics.
        if self._tts_cache_path(text).exists():
            logger.info("TTS cache-first hit: %s", text[:50])
            return self.speak_cached(
                text, interruptible=interruptible, realtime_feedback=realtime_feedback
            )

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
        self._realtime_feedback = realtime_feedback

        thread = threading.Thread(
            target=self._speak_sync,
            args=(text,),
            daemon=True,
            name="tts-speak",
        )
        thread.start()
        return True

    # Trailing unix-ms in a device run id, e.g. "device-chat-7-1788422075499".
    # Channel ids (tg-<messageID>) have none and fall through to the plain
    # sequence rule, which is what they had before.
    _RUN_ID_STAMP = re.compile(r"-(\d{13})$")

    @classmethod
    def _run_id_stamp(cls, turn_id: str):
        m = cls._RUN_ID_STAMP.search(turn_id or "")
        return int(m.group(1)) if m else None

    def _counter_restarted(self, turn_id: str, turn_seq: int) -> bool:
        """True when a lower-or-equal sequence belongs to a NEWER turn than the
        one holding the speaker — i.e. os-server restarted and began counting
        again.

        Both ids must carry a creation stamp: without one there is nothing to
        compare, and guessing would let a genuinely stale POST take the speaker
        back from the turn that superseded it.

        Equal sequences count. A restart resets the counter to 1, so the new
        run COLLIDES with the old high-water mark instead of falling under it
        whenever that mark is itself low — two restarts in a row, or a restart
        early in a session. Device-observed 04/09/2026: seq=2 met an unrelated
        seq=2 from before the restart and the wake greeting was dropped, the
        same silent-device symptom this guard exists to prevent. Wall clock
        settles it either way; only a same-millisecond tie falls through to the
        conflict rule below.
        """
        if turn_seq > self._latest_queue_turn_seq:
            return False
        incoming = self._run_id_stamp(turn_id)
        holding = self._run_id_stamp(self._latest_queue_turn_id)
        if incoming is None or holding is None:
            return False
        return incoming > holding

    def speak_queue(
        self,
        text: str,
        interruptible: bool = False,
        realtime_feedback: bool = False,
        turn_id: str = "",
        turn_seq: int = 0,
    ) -> bool:
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

        if self._speaker_muted():
            logger.info("TTS suppressed (queue) -- speaker muted: %s", text[:50])
            return False

        # Serialize the complete arrival path. A newer turn owns the speaker:
        # it stops the current utterance and clears pending older segments. A
        # delayed POST from an older turn is deliberately acknowledged but
        # dropped, so it cannot append itself after the replacement turn.
        with self._queue_request_lock:
            preempted = False
            if turn_id and turn_seq:
                # The counter lives in os-server; this threshold lives here.
                # They restart independently, so a deploy/OTA/crash restarts the
                # count at 1 while this process still holds the old high-water
                # mark — and every turn of the new session looks like a late
                # arrival from the past. Device-observed 03/09/2026: seq=1 against
                # latest_seq=40 silenced the wake greeting (LED and servo ran, no
                # sound), and would have stayed silent for the next 39 turns.
                #
                # The run id carries its creation time, so wall-clock decides
                # when the counter itself is untrustworthy: a turn created LATER
                # than the one holding the speaker is newer, whatever its seq.
                if self._counter_restarted(turn_id, turn_seq):
                    logger.warning(
                        "TTS turn counter restarted (turn_id=%s seq=%d <= latest_id=%s "
                        "latest_seq=%d, but newer) -- adopting the new sequence",
                        turn_id, turn_seq, self._latest_queue_turn_id,
                        self._latest_queue_turn_seq,
                    )
                    self._latest_queue_turn_seq = 0
                    self._latest_queue_turn_id = ""
                if turn_seq < self._latest_queue_turn_seq:
                    logger.info(
                        "TTS queued speech dropped -- superseded turn (turn_id=%s seq=%d latest_id=%s latest_seq=%d): %s",
                        turn_id, turn_seq, self._latest_queue_turn_id,
                        self._latest_queue_turn_seq, text[:60],
                    )
                    self._report_unspoken_reply(text, realtime_feedback)
                    return True
                if turn_seq == self._latest_queue_turn_seq and turn_id != self._latest_queue_turn_id:
                    logger.warning(
                        "TTS queued speech dropped -- conflicting turn sequence (turn_id=%s seq=%d latest_id=%s): %s",
                        turn_id, turn_seq, self._latest_queue_turn_id, text[:60],
                    )
                    self._report_unspoken_reply(text, realtime_feedback)
                    return True
                if turn_seq > self._latest_queue_turn_seq:
                    previous_id = self._latest_queue_turn_id
                    self._latest_queue_turn_id = turn_id
                    self._latest_queue_turn_seq = turn_seq
                    with self._pending_queue_lock:
                        has_pending = bool(self._pending_queue)
                    if self._speaking or has_pending:
                        logger.info(
                            "TTS newer turn preempting speaker (old_turn=%s new_turn=%s seq=%d)",
                            previous_id or "untracked", turn_id, turn_seq,
                        )
                        self.stop()
                        preempted = True

            # Cache-first — same rationale as speak(): exact-match warm phrases
            # (OS notices) must play without an API call, or a rate-limited
            # provider silences the very notice explaining the rate limit. Only
            # taken while idle; a busy strip queues normally below.
            if not self._speaking and self._tts_cache_path(text).exists():
                logger.info("TTS cache-first hit (queue): %s", text[:50])
                return self.speak_cached(
                    text, interruptible=interruptible, realtime_feedback=realtime_feedback
                )

            # A newer turn must take the lock itself after stopping the old
            # worker. Queuing it behind a set stop_event would strand it: the
            # interrupted worker correctly refuses to drain any queue. Same-
            # turn continuations retain the non-blocking queue behavior.
            if preempted:
                acquired = self._lock.acquire(blocking=True, timeout=2.0)
                if not acquired:
                    logger.warning("TTS lock not released after newer-turn preemption: %s", text[:50])
                    return False
            else:
                acquired = self._lock.acquire(blocking=False)

            if acquired:
                # Idle — start a normal speech (same as speak()).
                self._stop_event.clear()
                self._speaking = True
                self._interruptible = interruptible
                self._last_spoken_text = text
                self._realtime_feedback = realtime_feedback
                thread = threading.Thread(
                    target=self._speak_sync,
                    args=(text,),
                    daemon=True,
                    name="tts-speak-queue",
                )
                thread.start()
                return True

            # Busy — queue + kick off pre-synth so frames are ready when the
            # current speech ends. We don't try to interrupt segments of the
            # same turn; a newer turn was already handled above.
            item = _PendingSpeech(text=text, interruptible=interruptible)
            with self._pending_queue_lock:
                self._pending_queue.append(item)
                depth = len(self._pending_queue)
            threading.Thread(
                target=self._pre_synth_pending,
                args=(item,),
                daemon=True,
                name="tts-pre-synth",
            ).start()
            logger.info(
                "TTS queued for pre-synth (busy, queue depth=%d): %s",
                depth,
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

    # ── Native realtime playback (model's own voice, no synthesis) ──────────────

    def native_play_begin(self, src_rate: int) -> bool:
        """Begin streaming the realtime model's OWN float32 mono audio straight to
        the speaker, bypassing synthesis. Honors speaker mute and busy state.
        Pair with native_play_frame() then native_play_end(). Returns False if it
        can't start (unavailable / muted / busy)."""
        if not self.available or self._sd is None:
            return False
        if self._speaker_muted():
            logger.info("native audio suppressed -- speaker muted")
            return False
        if not self._lock.acquire(blocking=False):
            logger.info("native audio: speaker busy, skipping")
            return False
        self._stop_event.clear()
        self._native_src_rate = src_rate
        self._native_mode = True
        # Native playback is the realtime model's OWN voice — never feed it back
        # (the native_mode check in the hook already skips it; clear the flag too
        # so a stale True from a prior agent reply can't leak through).
        self._realtime_feedback = False
        self._speaking = True          # pauses silence keepalive + gates mic->STT
        self._interruptible = True     # stop()/barge-in can interrupt
        self._speak_start_fired = False
        return True

    def native_play_frame(self, frame) -> bool:
        """Write one float32 mono frame (resampled to the device stream rate).
        Returns False once playback is stopped/interrupted — caller should stop."""
        if not self._speaking or self._stop_event.is_set():
            return False
        np = self._np
        if not self._speak_start_fired and self._on_speak_start:
            self._speak_start_fired = True
            try:
                self._on_speak_start()
            except Exception:
                pass
        try:
            data = np.asarray(frame, dtype=np.float32)
            with self._stream_lock:
                stream = self._ensure_stream(self._device_rate)
                dst = self._stream_rate
                if self._native_src_rate and dst and self._native_src_rate != dst:
                    data = self._resample(data, self._native_src_rate, dst)
                if data.ndim == 1:
                    data = data.reshape(-1, 1)
                if self._stream is None or self._stop_event.is_set():
                    return False
                stream.write(data)
        except Exception as e:
            logger.warning("native audio write failed: %s", e)
            self._invalidate_stream()
            return False
        return True

    def native_play_end(self, transcript: str = "") -> None:
        """Finish native playback: release the speaker, record what was said for
        STT echo cancellation. Fires on_speak_end (LED restore) but keeps
        native_mode True across it so the realtime feedback hook skips re-feeding
        the model its own words."""
        self._speaking = False
        self._interruptible = False
        if transcript:
            self._last_spoken_text = transcript
        self._last_spoken_time = time.time()
        try:
            self._lock.release()
        except RuntimeError:
            pass
        if self._on_speak_end:
            try:
                self._on_speak_end()
            except Exception:
                pass
        self._native_mode = False

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
                # Rate limit / quota — retrying won't help; flag it so _speak_sync
                # announces the prerendered notice, and stop this chunk.
                if isinstance(e, TTSRateLimitError):
                    logger.warning("TTS rate-limited — skipping retries, will announce")
                    self._rate_limit_hit = True
                    break
                # Server-side errors (404, 503) — no point retrying or probing device
                status = getattr(e, "status_code", None)
                if status in (404, 503):
                    logger.warning("TTS server error %s — skipping retries", status)
                    break
                if attempt < self._max_retries:
                    self._probe_device_rate(force=True, use_cache=False)
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
                    if isinstance(e, TTSRateLimitError):
                        logger.warning("TTS rate-limited (head) — will announce")
                        self._rate_limit_hit = True
                        return
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
                        if isinstance(e, TTSRateLimitError):
                            logger.warning("TTS rate-limited (tail) — will announce")
                            self._rate_limit_hit = True
                            return
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
            self._forget_drain_queues()
            self._lock.release()
            return

        # _on_speak_start fires on first audio frame, not here — see _stream_chunk_with_retry
        self._speak_start_fired = False
        # Producers set this True on TTSRateLimitError; announced after playback.
        self._rate_limit_hit = False

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
        self._forget_drain_queues()
        self._register_drain_queue(head_q)
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
                        self._register_drain_queue(tail_q)
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
                    self._probe_device_rate(force=True, use_cache=False)
                    dst_rate = self._device_rate or TTS_SAMPLE_RATE
                    # Old head producer is at the stale rate -- orphan it and
                    # restart at the new rate. Daemon thread will exit on its own.
                    head_q = queue.Queue(maxsize=256)
                    self._forget_drain_queues()
                    self._register_drain_queue(head_q)
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
        # Before releasing the lock: a preempting turn takes it the instant it
        # frees up, and its queues must not be woken by our stop().
        self._forget_drain_queues()

        # Notify LED speaking effect — stop wave and restore previous LED state
        if self._on_speak_end:
            try:
                self._on_speak_end()
            except Exception:
                logger.exception("on_speak_end callback failed")

        self._lock.release()

        # Zero-sample completion = backend failure (all producer retries
        # gave up: Cloudflare 5xx, timeout, network drop). Flash amber so
        # the user knows their request was heard but the backend broke —
        # otherwise the device just goes silent and looks frozen. Skip when
        # the user explicitly stopped (stop_event set) — that's a normal
        # barge-in, not a failure.
        if total_samples == 0 and not self._stop_event.is_set():
            try:
                from hal import app_state

                app_state._flash_backend_error()
            except Exception:
                logger.exception("backend-error flash dispatch failed")

        # Lock is released before announcing so the notice can re-acquire it via
        # the cached-play path. Announce is a no-op unless a rate limit was hit.
        if self._rate_limit_hit:
            self._announce_rate_limit()

    def _announce_rate_limit(self) -> None:
        """Play the prerendered rate-limit notice so the user hears WHY the reply
        went silent, instead of nothing. Plays only from the WAV cache (never
        renders on miss) so it makes no API call while the provider is still
        rate-limited — and can't recurse into another rate-limit announce.
        Debounced: at most one notice per _RATE_LIMIT_ANNOUNCE_INTERVAL_S."""
        now = time.time()
        if now - self._last_rate_limit_announce < _RATE_LIMIT_ANNOUNCE_INTERVAL_S:
            logger.info("TTS rate-limit notice debounced")
            return
        try:
            from hal.i18n import PHRASE_RATE_LIMIT, localized_phrase

            phrase = localized_phrase(PHRASE_RATE_LIMIT)
        except Exception:
            logger.exception("Failed to resolve rate-limit phrase")
            return
        if not phrase:
            return
        if not self._tts_cache_path(phrase).exists():
            # Not prerendered (boot render failed or provider was already
            # rate-limited at boot) — stay silent rather than pay an API call.
            logger.warning("TTS rate-limit notice not in cache — staying silent")
            return
        self._last_rate_limit_announce = now
        logger.info("TTS announcing rate-limit notice")
        self.speak_cached(phrase)

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

    def warm_lifecycle_phrases(self) -> int:
        """Render the phrases the device says on its own initiative into the cache.

        These play at the worst possible moments. The restart notice is spoken
        while HAL is shutting down; the boot cue while every other service is
        still coming up. A cache miss there means loading a 63 MB Piper model on
        a CPU that is already saturated — measured on an 8-core sun60iw2, the
        model load alone is 2-3.4 s, and the restart phrase synthesises at 1.1x
        realtime, close enough to breaking even that a little extra load starves
        the audio stream and the speech comes out slurred.

        Rendering ahead costs one synthesis per phrase, once, off the hot path.
        The cache key includes provider and voice, so this has to run again
        whenever either changes — not only at boot.

        Returns how many phrases are now cached.
        """
        from hal.i18n import (
            PHRASE_REBOOT,
            PHRASE_SERVICE_RESTART,
            PHRASE_SHUTDOWN,
            PHRASE_SLEEP,
            localized_phrase,
        )

        warmed = 0
        for key in (PHRASE_SERVICE_RESTART, PHRASE_REBOOT, PHRASE_SHUTDOWN, PHRASE_SLEEP):
            text = localized_phrase(key)
            if not text:
                continue
            try:
                if self.speak_cached(text, prerender=True):
                    warmed += 1
            except Exception:
                logger.exception("Lifecycle phrase warm failed for %r", key)
        logger.info(
            "Lifecycle phrases warmed: %d cached (provider=%s, voice=%s)",
            warmed, self._provider, self._voice,
        )
        return warmed

    def speak_cached(self, text: str, interruptible: bool = False, prerender: bool = False, realtime_feedback: bool = False) -> bool:
        """Cache-aware speak. On hit -> ~50ms playback. On miss -> render+save
        then play. prerender=True skips playback (warmup-only).
        realtime_feedback=True feeds this text to the realtime agent on
        completion (agent replies only)."""
        if not self.available:
            logger.warning("TTS not available (cached path)")
            return False

        # Prerender only warms the cache (no playback), so it is NOT muted.
        if not prerender and self._speaker_muted():
            logger.info("TTS suppressed (cached) -- speaker muted: %s", text[:50])
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
        self._realtime_feedback = realtime_feedback

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

    def _ack_chime_samples(self, rate: int):
        """Pre-rendered acknowledgment ping: E6 + E7 sine partials, ~120ms,
        exponential decay, 5ms attack ramp (no onset click). Cached per
        stream rate; regenerated only if the device rate changes."""
        cached = self._ack_chime_cache
        if cached is not None and cached[0] == rate:
            return cached[1]
        np = self._np
        t = np.arange(int(rate * 0.12)) / rate
        envelope = np.exp(-t * 28.0)
        # 0.4 base: pure-sine pings read perceptually quieter than speech at
        # equal peak, so sit above typical TTS RMS. Backend volume_boost is
        # applied at play time (not baked in) so runtime boost changes and
        # this cache never disagree.
        tone = 0.4 * envelope * (
            np.sin(2 * np.pi * 1318.5 * t) + 0.5 * np.sin(2 * np.pi * 2637.0 * t)
        )
        samples = tone.astype(np.float32).reshape(-1, 1)
        attack = max(1, int(rate * 0.005))
        samples[:attack, 0] *= np.linspace(0.0, 1.0, attack, dtype=np.float32)
        self._ack_chime_cache = (rate, samples)
        return samples

    def play_ack_chime(self) -> bool:
        """Physical-gesture acknowledgment: write a short ping straight into
        the persistent output stream. Deliberately bypasses self._lock (the
        speak-level lock) — the ack must sound the instant a button/touch
        registers, not after the current utterance winds down. Callers stop
        TTS first; the playback loop then releases _stream_lock within one
        10ms block, so the chime serializes on _stream_lock only. Doesn't
        touch _speaking/_stop_event: 120ms of audio needs no stop support
        and must survive the just-set stop event. Returns False when the
        chime can't play (no audio, speaker muted, stream unopenable —
        e.g. while a music aplay owns the ALSA device exclusively)."""
        if not self.available or self._speaker_muted():
            return False
        if self._np is None or self._sd is None:
            return False
        try:
            rate = self._device_rate or TTS_SAMPLE_RATE
            samples = self._ack_chime_samples(rate)
            # Same software gain TTS playback applies (_play_wav_inline) so
            # the chime tracks perceived speech loudness, not just ALSA volume.
            if self._backend is not None:
                samples = self._np.clip(
                    samples * self._backend.volume_boost, -1.0, 1.0
                )
            with self._stream_lock:
                stream = self._ensure_stream(rate)
                stream.write(samples)
            return True
        except Exception as e:
            logger.debug("Ack chime failed: %s", e)
            return False

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
