"""
Backchannel — active listening cues during STT sessions.

Plays short filler words ("Uhm", "Ok", etc.) via TTS when the user pauses
mid-speech, signaling that Lamp is still listening.

Usage:
    bc = Backchannel(tts_service)
    bc.on_partial("hello I want to")   # call on every STT partial
    bc.reset()                          # call when STT session ends

Feature is disabled when LELAMP_BACKCHANNEL_FILLERS env var is empty.

Does NOT use tts_service.speak() — that would set the speaking flag and
kill the active STT session. Instead calls TTS API directly and plays
audio without touching the speaking flag.

Config (env vars):
    LELAMP_BACKCHANNEL_FILLERS     comma-separated filler words (empty = disabled)
    LELAMP_BACKCHANNEL_STALL_S     partial unchanged for N seconds → play cue (0 = every partial)
    LELAMP_BACKCHANNEL_INTERVAL_S  min seconds between cues
    LELAMP_BACKCHANNEL_VOLUME      volume multiplier 0.0–1.0
"""

import logging
import math
import os
import random
import threading
import time
from typing import Optional

from lelamp.i18n import DEFAULT_FILLERS_BY_LANG
from lelamp.presets import DEFAULT_LANG

logger = logging.getLogger("lelamp.voice.backchannel")


def _default_fillers_for_active_lang() -> str:
    """Pick the default filler list based on Lamp's stt_language. Falls
    back to DEFAULT_LANG when the config can't be read or the language is
    empty/unknown. Caller can still override with LELAMP_BACKCHANNEL_FILLERS."""
    try:
        from lelamp.config import _lamp_cfg_get
        lang = (_lamp_cfg_get("stt_language") or "").strip()
    except Exception:
        lang = ""
    return DEFAULT_FILLERS_BY_LANG.get(lang, DEFAULT_FILLERS_BY_LANG[DEFAULT_LANG])


# Comma-separated filler words to play as listening cues. Empty string = feature disabled.
_fillers_env = os.environ.get("LELAMP_BACKCHANNEL_FILLERS", _default_fillers_for_active_lang())
FILLERS = [w.strip() for w in _fillers_env.split(",") if w.strip()]
# How long (seconds) the partial transcript must stay unchanged before playing a cue.
# 0 = play on every new partial (still throttled by MIN_INTERVAL_S).
# Kept at 8s so backchannel fires only on real multi-second silence
# (user lost their train of thought) rather than on every natural
# breath-pause mid-sentence — those short pauses are what the speaker
# recognizer needs clean, and backchannel audio bleeds into the mic.
STALL_TIMEOUT_S = float(os.environ.get("LELAMP_BACKCHANNEL_STALL_S", "8.0"))
# Minimum seconds between two consecutive cues (prevents spamming).
MIN_INTERVAL_S = float(os.environ.get("LELAMP_BACKCHANNEL_INTERVAL_S", "5.0"))
# Volume multiplier for cue audio relative to normal TTS (0.0 = silent, 1.0 = full).
# Kept at 0.5 so backchannel bleed doesn't saturate the mic and corrupt the
# speaker-ID embedding of whoever is still talking.
VOLUME = float(os.environ.get("LELAMP_BACKCHANNEL_VOLUME", "0.5"))


class Backchannel:
    """Monitor STT partials and play filler words when user pauses mid-speech."""

    def __init__(self, tts_service):
        self._tts = tts_service
        self._last_partial: str = ""
        self._last_cue_time: float = 0.0
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None

        if FILLERS:
            logger.info("Backchannel enabled: fillers=%s, stall=%.1fs, interval=%.1fs",
                        FILLERS, STALL_TIMEOUT_S, MIN_INTERVAL_S)

    @property
    def enabled(self) -> bool:
        return len(FILLERS) > 0

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
        import lelamp.app_state as _state
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
            with tts._stream_lock:
                stream = tts._ensure_stream(dst_rate)
                stream.write(samples_2d)
            logger.info("Backchannel played: '%s'", text)
        except Exception as e:
            logger.warning("Backchannel play failed: %s", e)
