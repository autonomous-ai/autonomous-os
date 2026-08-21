"""
Backchannel — active listening cues during STT sessions.

Plays short filler words ("Uhm", "Ok", etc.) via TTS when the user pauses
mid-speech, signaling that the agent is still listening.

Usage:
    bc = Backchannel(tts_service)
    bc.on_partial("hello I want to")   # call on every STT partial
    bc.reset()                          # call when STT session ends

Feature is disabled when HAL_BACKCHANNEL_FILLERS env var is empty.

Does NOT use tts_service.speak() — that would set the speaking flag and
kill the active STT session. Instead calls TTS API directly and plays
audio without touching the speaking flag.

Config (env vars):
    HAL_BACKCHANNEL_FILLERS     comma-separated filler words (empty = disabled)
    HAL_BACKCHANNEL_STALL_S     partial unchanged for N seconds → play cue (0 = every partial)
    HAL_BACKCHANNEL_INTERVAL_S  min seconds between cues
    HAL_BACKCHANNEL_VOLUME      volume multiplier 0.0–1.0
    HAL_BACKCHANNEL_ECHO_TAIL_S seconds after playback the mic still ignores itself
"""

import logging
import math
import os
import random
import threading
import time
from typing import Optional

from hal.i18n import DEFAULT_FILLERS_BY_LANG
from hal.presets import DEFAULT_LANG

logger = logging.getLogger("hal.voice.backchannel")


def _default_fillers_for_active_lang() -> str:
    """Pick the default filler list based on the OS server's stt_language. Falls
    back to DEFAULT_LANG when the config can't be read or the language is
    empty/unknown. Caller can still override with HAL_BACKCHANNEL_FILLERS."""
    try:
        from hal.config import _os_cfg_get
        lang = (_os_cfg_get("stt_language") or "").strip()
    except Exception:
        lang = ""
    return DEFAULT_FILLERS_BY_LANG.get(lang, DEFAULT_FILLERS_BY_LANG[DEFAULT_LANG])


# Comma-separated filler words to play as listening cues. Empty string = feature disabled.
_fillers_env = os.environ.get("HAL_BACKCHANNEL_FILLERS", _default_fillers_for_active_lang())
FILLERS = [w.strip() for w in _fillers_env.split(",") if w.strip()]
# How long (seconds) the partial transcript must stay unchanged before playing a cue.
# 0 = play on every new partial (still throttled by MIN_INTERVAL_S).
# Kept at 8s so backchannel fires only on real multi-second silence
# (user lost their train of thought) rather than on every natural
# breath-pause mid-sentence — those short pauses are what the speaker
# recognizer needs clean, and backchannel audio bleeds into the mic.
STALL_TIMEOUT_S = float(os.environ.get("HAL_BACKCHANNEL_STALL_S", "8.0"))
# Minimum seconds between two consecutive cues (prevents spamming).
MIN_INTERVAL_S = float(os.environ.get("HAL_BACKCHANNEL_INTERVAL_S", "5.0"))
# Volume multiplier for cue audio relative to normal TTS (0.0 = silent, 1.0 = full).
# Kept at 0.5 so backchannel bleed doesn't saturate the mic and corrupt the
# speaker-ID embedding of whoever is still talking.
VOLUME = float(os.environ.get("HAL_BACKCHANNEL_VOLUME", "0.5"))
# Extra seconds after cue playback during which the mic still counts as hearing
# our own audio. Covers speaker/room reverb tail, which outlasts the samples.
ECHO_TAIL_S = float(os.environ.get("HAL_BACKCHANNEL_ECHO_TAIL_S", "0.4"))


class Backchannel:
    """Monitor STT partials and play filler words when user pauses mid-speech."""

    def __init__(self, tts_service):
        self._tts = tts_service
        self._last_partial: str = ""
        self._last_cue_time: float = 0.0
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        # Monotonic deadline until which our own cue audio is in the room.
        self._self_audio_until: float = 0.0

        if FILLERS:
            logger.info("Backchannel enabled: fillers=%s, stall=%.1fs, interval=%.1fs",
                        FILLERS, STALL_TIMEOUT_S, MIN_INTERVAL_S)

    @property
    def enabled(self) -> bool:
        return len(FILLERS) > 0

    @property
    def self_audio_active(self) -> bool:
        """True while a cue is playing (plus reverb tail).

        Deliberately NOT the TTS `speaking` flag: that flag ends the running STT
        session, which is exactly what backchannel must not do. This one only
        tells the VAD loop that what it hears right now is us, so it must not
        OPEN a new session on it. Device-observed 19/08/2026: without this, every
        cue spawned a phantom session ~1s later whose transcript was the cue
        itself ('Ok' → 'Okay.', 'Oh' → 'no'), which then ran as a real turn.
        """
        return time.monotonic() < self._self_audio_until

    def on_partial(self, text: str) -> None:
        """Called on each STT partial. Schedules a cue if partial stalls."""
        if not FILLERS:
            return
        with self._lock:
            if text == self._last_partial:
                return
            self._last_partial = text
            self._cancel_timer()
            if STALL_TIMEOUT_S <= 0:
                self._fire_cue()
            else:
                self._timer = threading.Timer(STALL_TIMEOUT_S, self._fire_cue)
                self._timer.daemon = True
                self._timer.start()

    def reset(self) -> None:
        """Reset state when STT session ends."""
        with self._lock:
            self._cancel_timer()
            self._last_partial = ""

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _fire_cue(self) -> None:
        """Check interval, then play a random filler in background thread."""
        now = time.time()
        if (now - self._last_cue_time) < MIN_INTERVAL_S:
            logger.info("Backchannel skipped: interval cooldown (%.1fs < %.1fs)",
                        now - self._last_cue_time, MIN_INTERVAL_S)
            return
        if not self._last_partial.strip():
            return
        if self._tts is not None and self._tts.speaking:
            logger.info("Backchannel skipped: TTS is speaking")
            return
        self._last_cue_time = now
        filler = random.choice(FILLERS)
        logger.info("Backchannel: '%s'", filler)
        threading.Thread(target=self._play, args=(filler,), daemon=True, name="bc-cue").start()

    def _play(self, text: str) -> None:
        """Play a short TTS cue directly, bypassing tts_service.speak()."""
        import hal.app_state as _state
        if _state._speaker_muted:
            return
        tts = self._tts
        if tts is None or tts._backend is None or not tts._backend.available or tts._sd is None:
            return
        try:
            import numpy as np
            dst_rate = tts._device_rate or 24000
            src_rate = tts._backend.sample_rate
            raw = b""
            for chunk in tts._backend.stream_pcm(
                text=text,
                voice=tts._voice,
                model=tts._model,
                speed=tts._speed,
            ):
                raw += chunk
            if len(raw) < 2:
                return
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            samples *= max(0.0, min(1.0, VOLUME))
            if dst_rate != src_rate:
                ratio = dst_rate / src_rate
                n_out = math.ceil(len(samples) * ratio)
                x_old = np.linspace(0, 1, len(samples))
                x_new = np.linspace(0, 1, n_out)
                samples = np.interp(x_new, x_old, samples).astype(np.float32)
            # Reuse the TTS persistent stream — the device is held exclusively
            # so opening a second OutputStream returns PaErrorCode -9985.
            samples_2d = samples.reshape(-1, 1)
            # Arm the self-audio window BEFORE the first sample leaves, and size
            # it from the actual clip length: stream.write() blocks until the
            # audio is consumed, so arming afterwards would already be too late
            # for the VAD loop running in parallel.
            duration_s = len(samples) / float(dst_rate)
            with self._lock:
                self._self_audio_until = time.monotonic() + duration_s + ECHO_TAIL_S
            try:
                with tts._stream_lock:
                    stream = tts._ensure_stream(dst_rate)
                    stream.write(samples_2d)
            finally:
                # Re-anchor the tail to when playback actually ended — waiting on
                # _stream_lock can push real playback past the estimate above.
                with self._lock:
                    self._self_audio_until = max(
                        self._self_audio_until, time.monotonic() + ECHO_TAIL_S
                    )
            logger.info(
                "Backchannel played: '%s' (%.2fs, mic self-audio window +%.1fs)",
                text, duration_s, ECHO_TAIL_S,
            )
        except Exception as e:
            logger.warning("Backchannel play failed: %s", e)
