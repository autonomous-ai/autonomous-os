"""
Voice Service — local VAD + pluggable STT for autonomous sensing.

Pipeline:
  1. Mic always on, local RMS energy check (free, zero cost)
  2. Speech detected → create STT session, stream audio
  3. Silence for SILENCE_TIMEOUT → close session (stop billing)
  4. Transcripts → POST to OS server /api/sensing/event
  5. OS server → local intent match or OpenClaw → AI responds → POST /voice/speak

STT provider is pluggable (default: Deepgram).

Helpers live in `_internal/` — config constants, audio I/O, VAD filters,
speaker decoration, and OS server event sender.
"""

import logging
import re
import subprocess
import threading
import time
from collections import deque
from difflib import SequenceMatcher
from typing import Optional

import requests

from hal import config as hal_config
from hal import presets
from hal.realtime.enums import AgentGateway
from hal.realtime.orchestrator import RealtimeOrchestrator
from hal.realtime.utils import pcm16_bytes_to_float32, resample_float32
from hal.drivers.voice._internal import config as voice_cfg
from hal.drivers.voice._internal.audio_dsp import resample_to_stt, rms
from hal.drivers.voice._internal.audio_recorder import ArecordStream
from hal.drivers.voice._internal.realtime_turn import (
    RealtimeTurnResult,
    build_speaker_correction,
    build_turn_context,
    is_noise_turn,
    run_realtime_turn,
    should_dispatch_to_main,
)
from hal.drivers.voice._internal.sensing_sender import SensingSender
from hal.drivers.voice._internal.session_finalize import finalize_session
from hal.drivers.voice._internal.speaker_decorate import (
    SpeakerDecorator,
    merge_stt_hypothesis,
    merge_wake_words,
)
from hal.drivers.voice._internal.turn_dispatch import dispatch_turn
from hal.drivers.voice._internal.vad_filters import SileroVADFilter, WebRTCVADFilter
from hal.drivers.voice._internal.wakeword_focus import WakeWordFocus
from hal.drivers.voice.backchannel import Backchannel
from hal.drivers.voice.stt import STTProvider

logger = logging.getLogger("hal.voice")

# Below this difflib ratio, a SHORTER final is a new turn, not a correction.
# (~0.05–0.10 unrelated vs ~0.74–0.88 self-correction; 0.5 splits them cleanly.)
_TRANSCRIPT_MIN_SIMILARITY = 0.5


def _is_normal_ws_close(error: Exception) -> bool:
    """Whether an STT exception represents a peer's normal WS close (1000)."""
    code = getattr(error, "code", None)
    received = getattr(error, "rcvd", None)
    if code is None and received is not None:
        code = getattr(received, "code", None)
    return code == 1000 or "received 1000 (OK)" in str(error)


class VoiceService:
    """Local VAD + pluggable STT provider for autonomous sensing."""

    # Strip HW markers, audio tags, and system tags from realtime agent output.
    # The HW alternative mirrors the Go executor grammar (handler_hw.go
    # hwMarkerRe): brace-anchored optional body, so a `]` inside a JSON array
    # body (e.g. {"color":[255,0,0]}) doesn't truncate the match.
    RT_MARKER_RE: re.Pattern[str] = re.compile(
        r"\[HW:/[^{\]]*(?:\{[^}]*\})?\]"
        r"|\[(?:laughs|LAUGHS|sighs|chuckle|light chuckle|giggle|big laugh|gasps|gulps|breathes|clears throat|whispers|pause|pauses|hesitates|stammers|thinking|thinks|thought|thoughtful|pondering|ponders|reasoning)"
        r"[^\]]*\]"
        r"|\[(?:cheerfully|playfully|quietly|nervously|deadpan|flatly|dramatic tone|resigned tone|excited|calm|tired|sad|sorrowful|nervous|frustrated)"
        r"[^\]]*\]"
        r"|`\[[^\]]*\]`"
        r"|/(?:emotion|servo|led|skills)[^\s]*"
        # Bare emotion-annotation prefix the realtime model sometimes emits and
        # then mimics from its own saved history (e.g.
        # "emotion_user:concentration intensity:1.0 emotion_model:calm intensity:1.0 …").
        # It has no brackets/slash so the markers above miss it; strip each token so
        # it never reaches TTS NOR the saved transcript (which breaks the loop).
        r"|emotion_(?:user|model)\s*:\s*\S+"
        r"|\bintensity\s*:\s*[0-9.]+"
        r"|NO_REPLY",
        re.IGNORECASE,
    )

    # Markdown-link-form HW marker like [Lights off](HW:/led/off:{}) — some LLMs
    # wrap the marker in a link. Keep the label, drop the marker. Mirrors
    # hwLinkRe in os-server handler_hw.go EXACTLY: never looser than the
    # executor, so a variant it won't fire stays visible as raw text instead
    # of being scrubbed into a confident-looking label.
    RT_HW_LINK_RE: re.Pattern[str] = re.compile(
        r"\[([^\]]*)\]\(\s*HW:\s*(?:/[^(){:\s]+(?::[^(){:\s]+)*)(?::\{[^}]*\})?:?\s*\)",
        re.IGNORECASE,
    )

    @staticmethod
    def strip_rt_markers(text: str) -> str:
        """Remove HW markers, audio tags, and system tags from realtime agent text."""
        # Label may itself be a canonical marker's content (LLM link-wrapped
        # the second of a back-to-back pair) — both are markers, keep neither.
        text = VoiceService.RT_HW_LINK_RE.sub(
            lambda m: "" if m.group(1)[:3].lower() == "hw:" else m.group(1), text
        )
        cleaned: str = VoiceService.RT_MARKER_RE.sub("", text)
        cleaned = re.sub(r"  +", " ", cleaned).strip()
        return cleaned

    def __init__(
        self,
        stt_provider: STTProvider,
        input_device: Optional[int] = None,
        tts_service=None,
        music_service=None,
        wake_words: Optional[list] = None,
        alsa_device: Optional[str] = None,
        enable_people_perception: bool = True,
        enable_expression: bool = False,
    ):
        self._stt = stt_provider
        self._input_device = input_device
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._listening = False
        # Latest mic frame RMS (int16 scale) + capture timestamp — published by
        # the capture loops below, read by GET /voice/mic-level for the web VU
        # meter. Plain float writes are atomic under the GIL, no lock needed.
        self._mic_level = 0.0
        self._mic_level_ts = 0.0
        # When STT last produced transcript text (partial or final) — proof
        # that the loud audio in the room is PEOPLE TALKING, not noise. Read
        # by SoundPerception: on the saturating sensing mic, conversation
        # (~740 RMS) is indistinguishable from thunder (~755) by level, but
        # speech transcribes and thunder comes back empty.
        self._last_transcript_ts = 0.0
        self._tts = tts_service
        self._music = music_service
        self._device_rate: Optional[int] = None  # detected once at first use

        self._sd = None
        self._np = None
        # Explicit override from .env → skip auto-detection entirely
        self._alsa_device: Optional[str] = alsa_device or None

        self._backchannel = Backchannel(tts_service)

        try:
            import numpy as np

            self._np = np
        except ImportError:
            logger.warning("numpy not available for voice")

        try:
            import sounddevice as sd

            self._sd = sd
        except ImportError:
            logger.warning("sounddevice not available")

        # WebRTC VAD — fast C-based pre-filter (~0.1ms vs Silero ~20ms).
        # Enable via HAL_WEBRTCVAD_ENABLED=true in .env.
        self._webrtc_vad = (
            WebRTCVADFilter(voice_cfg.WEBRTCVAD_AGGRESSIVENESS, self._np)
            if voice_cfg.WEBRTCVAD_ENABLED
            else None
        )
        if not voice_cfg.WEBRTCVAD_ENABLED:
            logger.info("WebRTC VAD disabled (HAL_WEBRTCVAD_ENABLED=false)")

        self._silero_vad = (
            SileroVADFilter(voice_cfg.SILERO_MODEL_PATH, self._np) if voice_cfg.SILERO_VAD_ENABLED else None
        )
        if not voice_cfg.SILERO_VAD_ENABLED:
            logger.info("Silero VAD disabled via HAL_SILERO_ENABLED=false")
        # Dedicated Silero instance for the realtime empty-STT noise guard, built
        # lazily on first use. Kept SEPARATE from the entry-gate VAD above so it
        # works even when HAL_SILERO_ENABLED is false (the common case) and never
        # shares LSTM state with the entry gate. See _rt_noise_is_speech.
        self._rt_noise_vad: SileroVADFilter | None = None
        # Third instance, for the silence clock (see SILENCE_VAD_ENABLED). Same
        # reason as the guard above: its own LSTM state, so confirming "is this
        # noise or speech" mid-capture cannot disturb the entry gate's state,
        # and it works whether or not the entry gate is enabled.
        self._silence_vad: SileroVADFilter | None = None

        # Speaker decoration (wake-word + speaker recognizer + SER). Speaker-ID and
        # SER (speech emotion) are voice people-perception — gated on the `audio`
        # capability (the mic), passed in via enable_people_perception.
        # "Autonomous" and device type are permanent spoken aliases ("hey
        # autonomous", "hey lamp"); the runtime's current agent name is an
        # additional alias ("hey Luna"). Runtime rename updates must never
        # replace the permanent aliases.
        self._device_wake_words = list(voice_cfg.DEFAULT_WAKE_WORDS)
        self._decorator = SpeakerDecorator(
            wake_words=merge_wake_words(self._device_wake_words, wake_words or []),
            nudge_cooldown_s=voice_cfg.ENROLL_NUDGE_COOLDOWN_S,
            enable_people_perception=enable_people_perception,
        )
        # Unlike per-session wake_word_confirmed, this small focus window is
        # shared across mic sessions so a user can naturally continue a
        # wake-word conversation without reopening the gate on every sentence.
        self._wakeword_focus = WakeWordFocus(hal_config.WAKEWORD_FOLLOWUP_TIMEOUT_S)

        # OS server event sender (with echo similarity filter)
        self._sensing_sender = SensingSender(tts_service=tts_service)

        # Realtime voice agent — parallel audio pipeline (Gemini Live / OpenAI Realtime).
        self._realtime = RealtimeOrchestrator(
            gateway=AgentGateway(hal_config.AGENT_GATEWAY),
            enable_expression=enable_expression,
        )

        # Hook into TTS on_speak_end to feed spoken text back to the realtime agent.
        # With turn_complete=False on text inputs, this won't trigger a standalone response.
        if tts_service is not None:
            original_on_speak_end = tts_service._on_speak_end

            def _tts_speak_end_with_realtime_feedback() -> None:
                if original_on_speak_end:
                    original_on_speak_end()
                if (
                    hal_config.REALTIME_ENABLED
                    and tts_service.last_spoken_text
                    and not tts_service.native_mode
                    and tts_service.realtime_feedback
                ):
                    text: str = tts_service.last_spoken_text
                    # Direction is INTO the realtime model: whatever was just
                    # spoken (often an OpenClaw reply, not Gemini's own output) is
                    # pushed to Gemini as history so it stays aware of what the
                    # device said and won't repeat it. Not a Gemini-generated line.
                    # Capped: it accumulates in session context and is re-billed
                    # on every later turn until recycle — the gist is enough to
                    # avoid repetition.
                    max_hist = hal_config.REALTIME_TTS_HISTORY_MAX_CHARS
                    if len(text) > max_hist:
                        text = text[:max_hist] + "…"
                    logger.info(
                        "[realtime<-tts] Notifying realtime agent of spoken text: %r",
                        text[:100],
                    )
                    self._realtime.send_text(f"[TTS HISTORY] {text}")

            tts_service._on_speak_end = _tts_speak_end_with_realtime_feedback

    def set_music_service(self, music_service) -> None:
        self._music = music_service

    def grant_wakeword_focus(self, source: str = "button") -> bool:
        """Open the wake-word follow-up window without a spoken wake phrase.

        A single click is a "give me the floor" gesture: the device stops
        talking and announces it is listening, so requiring the user to say
        the wake phrase right after would contradict the cue. Granting the
        same focus window a wake word grants makes the click a wake event.
        No-op when wake word is off (every utterance already dispatches) or
        when follow-up focus is disabled (timeout 0)."""
        if not hal_config.WAKEWORD_ENABLED:
            return False
        if self._wakeword_focus.refresh():
            logger.info(
                "%s -- wake-word focus granted for %.0fs",
                source,
                hal_config.WAKEWORD_FOLLOWUP_TIMEOUT_S,
            )
            return True
        return False

    def set_wake_words(self, words: list) -> None:
        """Update wake word list at runtime (called when agent is renamed)."""
        self._decorator.set_wake_words(
            merge_wake_words(self._device_wake_words, words)
        )

    @staticmethod
    def _set_emotion_local(emotion: str) -> None:
        """Set a device emotion by calling the HAL handler in-process.

        VoiceService runs inside the HAL process, so we call the route handler
        directly instead of an HTTP loopback to our own :5001/emotion — no
        serialization, no network stack. (Cross-process calls to the os-server
        on :5000 stay over HTTP — those are a different process.)
        """
        try:
            from hal.models import EmotionRequest
            from hal.routes.emotion import express_emotion

            express_emotion(EmotionRequest(emotion=emotion))
        except Exception as e:
            logger.warning("emotion '%s' trigger failed: %s", emotion, e)

    @property
    def available(self) -> bool:
        return self._sd is not None and self._np is not None and self._stt.available

    @property
    def listening(self) -> bool:
        return self._listening

    @property
    def last_transcript_ts(self) -> float:
        """Unix ts of the last non-empty STT transcript (partial or final).
        0.0 until someone has spoken. See _last_transcript_ts above."""
        return self._last_transcript_ts

    @property
    def mic_level(self) -> float:
        """Latest mic input RMS (int16 scale, 0..32768).

        Returns 0.0 when the reading is stale (>1s old) — e.g. while the mic
        drains under TTS/music playback or the capture loop is paused — so the
        VU meter falls to zero instead of freezing at the last value.
        """
        if (time.time() - self._mic_level_ts) > 1.0:
            return 0.0
        return self._mic_level

    def start(self):
        if self._running:
            return
        if not self.available:
            logger.warning(
                "VoiceService not starting — sd=%s np=%s stt=%s",
                self._sd is not None,
                self._np is not None,
                self._stt.available,
            )
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="voice")
        self._thread.start()
        logger.info("VoiceService started (local VAD + %s)", self._stt.name)

    def stop(self):
        self._running = False
        if hal_config.REALTIME_ENABLED:
            # realtime.stop() calls _context.summarize_device_memory() +
            # summarize_realtime_memory() which fire LLM requests — an
            # unresponsive backend (Cloudflare 524, network stall) can hang
            # them for tens of seconds and stall the entire voice teardown.
            # Wrap in a daemon thread with a bounded join so the summarize
            # is best-effort: if it doesn't finish in 3s we orphan it and
            # continue teardown. Better to lose one summary than to leave
            # the whole voice pipeline stuck waiting.
            rt_thread = threading.Thread(
                target=self._realtime.stop,
                daemon=True,
                name="voice-realtime-teardown",
            )
            rt_thread.start()
            rt_thread.join(timeout=3.0)
            if rt_thread.is_alive():
                logger.warning(
                    "realtime.stop() did not finish in 3s -- orphaning (memory summary or WS disconnect stalled)"
                )
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("VoiceService stopped")

    # ------------------------------------------------------------------
    # Audio device discovery
    # ------------------------------------------------------------------
    def _get_alsa_device_str(self) -> Optional[str]:
        """Derive ALSA plughw device string from the sounddevice input device index.

        sounddevice device names on Linux usually contain '(hw:X,Y)' which maps
        directly to the underlying ALSA card. Returns e.g. 'plughw:1,0'.
        Falls back to parsing `arecord -l` if the name has no hw: token.
        """
        if self._input_device is None or self._sd is None:
            return None
        try:
            name = self._sd.query_devices(self._input_device)["name"]
            import re as _re

            m = _re.search(r"\(hw:(\d+),(\d+)\)", name)
            if m:
                alsa = f"plughw:{m.group(1)},{m.group(2)}"
                logger.info("ALSA device: %s (from sd device name '%s')", alsa, name)
                return alsa
        except Exception as e:
            logger.debug("Could not extract hw: from sd device name: %s", e)

        # Fallback: first card from `arecord -l`
        try:
            result = subprocess.run(
                ["arecord", "-l"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                import re as _re

                for line in result.stdout.splitlines():
                    if line.startswith("card "):
                        m = _re.search(r"card (\d+):", line)
                        if m:
                            alsa = f"plughw:{m.group(1)},0"
                            logger.info("ALSA device: %s (from arecord -l)", alsa)
                            return alsa
        except Exception as e:
            logger.debug("arecord -l failed: %s", e)

        return None

    def _detect_device_rate(self) -> int:
        """Detect the highest-quality sample rate the input device supports.
        Tries STT_RATE first (ideal), then falls back to device native rate."""
        sd = self._sd
        try:
            info = sd.query_devices(self._input_device, "input")
            native = int(info["default_samplerate"])
            # Try to open stream at STT_RATE directly — ALSA plughw does SRC transparently.
            try:
                with sd.InputStream(
                    device=self._input_device,
                    samplerate=voice_cfg.STT_RATE,
                    channels=voice_cfg.CHANNELS,
                    dtype="int16",
                    blocksize=512,
                ):
                    pass
                logger.info(
                    "Audio device opened at %dHz natively (no resample needed)",
                    voice_cfg.STT_RATE,
                )
                return voice_cfg.STT_RATE
            except Exception:
                logger.info(
                    "Audio device native rate: %dHz (will resample to %dHz for STT)",
                    native,
                    voice_cfg.STT_RATE,
                )
                return native
        except Exception as e:
            logger.warning(
                "Could not detect device rate, defaulting to %dHz: %s", voice_cfg.STT_RATE, e
            )
            return voice_cfg.STT_RATE

    # ------------------------------------------------------------------
    # VAD helpers — thin wrappers that fail-open when filter is None
    # ------------------------------------------------------------------
    def _webrtcvad_is_speech(self, data, device_rate: int) -> bool:
        """Run WebRTC VAD on `data` (normal STT path). True if speech or filter off."""
        if self._webrtc_vad is None:
            return True
        return self._webrtc_vad.is_speech(data, device_rate)

    def _silero_is_speech(self, data, device_rate: int) -> bool:
        """Run Silero VAD on `data`. True if speech or filter off."""
        if self._silero_vad is None:
            return True
        return self._silero_vad.is_speech(data, device_rate)

    def _silero_reset_state(self) -> None:
        if self._silero_vad is not None:
            self._silero_vad.reset_state()

    def _rt_noise_is_speech(self, pcm_int16) -> bool:
        """Realtime noise guard: is `pcm_int16` (STT_RATE PCM16 samples) speech?

        Uses a dedicated, lazily-built Silero instance — independent of
        HAL_SILERO_ENABLED — so the empty-STT noise filter works regardless of
        which VAD the entry gate runs. Fails open (returns True = treat as speech,
        commit) on any error so a model glitch never drops a real turn. Resets the
        LSTM state after each call (each turn is judged independently)."""
        if self._rt_noise_vad is None:
            try:
                self._rt_noise_vad = SileroVADFilter(
                    voice_cfg.SILERO_MODEL_PATH, self._np
                )
            except Exception as e:
                logger.warning("Realtime noise-guard Silero load failed: %s", e)
                return True
        try:
            peak, mean, ratio = self._rt_noise_vad.speech_metrics(
                pcm_int16, voice_cfg.STT_RATE
            )
            self._rt_noise_vad.reset_state()
            # Judge by VOICED RATIO, not peak: a real speaking turn is voiced
            # across most of its length; sustained noise only spikes sparsely.
            # (is_speech is peak-only — one transient chunk would pass noise.)
            is_speech = ratio >= hal_config.REALTIME_NOISE_SPEECH_RATIO
            logger.info(
                "[realtime] noise-guard metrics: peak=%.3f mean=%.3f voiced_ratio=%.3f "
                "(>= %.2f? %s)",
                peak, mean, ratio,
                hal_config.REALTIME_NOISE_SPEECH_RATIO, is_speech,
            )
            return is_speech
        except Exception as e:
            logger.warning("Realtime noise-guard Silero inference failed: %s", e)
            return True

    def _silence_window_is_speech(self, window, device_rate: int) -> bool:
        """Is this above-RMS window real speech, or just a loud room?

        Answers the one question the silence clock needs: should this window
        refresh the timer. Keeps its LSTM state ACROSS calls within a session
        (unlike the per-turn guard above) — the window is a continuation of the
        same utterance, so the state carries useful context; the caller resets
        it at session start.

        Fails open (True) on any error: a model glitch must never cut somebody
        off mid-sentence. That direction of failure only costs the old
        RMS-only behavior, which is what we already had.
        """
        if self._silence_vad is None:
            try:
                self._silence_vad = SileroVADFilter(
                    voice_cfg.SILERO_MODEL_PATH, self._np
                )
            except Exception as e:
                logger.warning("Silence-clock Silero load failed: %s", e)
                return True
        if not self._silence_vad.available:
            return True
        try:
            return self._silence_vad.is_speech(window, device_rate)
        except Exception as e:
            logger.warning("Silence-clock Silero inference failed: %s", e)
            return True

    # ------------------------------------------------------------------
    # State checks
    # ------------------------------------------------------------------
    def _tts_is_speaking(self) -> bool:
        """Check if TTS is currently using the audio device."""
        return self._tts is not None and self._tts.speaking

    def _music_is_playing(self) -> bool:
        """Check if music is currently playing."""
        return self._music is not None and self._music.playing

    # ------------------------------------------------------------------
    # TTS wait + reverb gate (Layer 1 + Layer 2 echo handling)
    # ------------------------------------------------------------------
    def _wait_for_tts(self):
        """Block until TTS finishes speaking, then wait for reverb to decay (adaptive RMS gate).

        When BARGE_IN_ENABLED, the passive wait is replaced by an active mic monitor
        that interrupts TTS on user voice. After barge-in the reverb gate is skipped
        because the user is mid-utterance — waiting for silence would clip them.
        """
        if not self._tts_is_speaking():
            return

        barged_in = False
        if voice_cfg.BARGE_IN_ENABLED:
            barged_in = self._monitor_barge_in()
        else:
            logger.info("TTS is speaking, pausing mic until done...")
            while self._running and self._tts_is_speaking():
                time.sleep(0.2)

        if not self._running:
            return
        if barged_in:
            logger.info("Barge-in fired: skipping reverb gate, opening mic immediately")
            return

        # Adaptive RMS gate: wait for reverb/echo to decay instead of fixed sleep
        logger.info("TTS done, waiting for reverb decay (RMS < %d)...", voice_cfg.ECHO_RMS_FLOOR)
        np = self._np
        device_rate = self._device_rate or voice_cfg.STT_RATE
        window_frames = int(device_rate * voice_cfg.ECHO_GATE_WINDOW_S)
        try:
            # Prefer arecord backend (same as recording loop) — avoids PortAudio rate errors
            if self._alsa_device is not None:
                mic_ctx = ArecordStream(
                    alsa_device=self._alsa_device,
                    rate=device_rate,
                    channels=voice_cfg.CHANNELS,
                    blocksize=window_frames,
                    np=np,
                )
            else:
                mic_ctx = self._sd.InputStream(
                    samplerate=device_rate,
                    channels=voice_cfg.CHANNELS,
                    dtype="int16",
                    blocksize=window_frames,
                    device=self._input_device,
                )
            elapsed = 0.0
            with mic_ctx as tmp_mic:
                while elapsed < voice_cfg.ECHO_GATE_MAX_WAIT_S and self._running:
                    data, overflowed = tmp_mic.read(window_frames)
                    if overflowed:
                        continue
                    measured = float(np.sqrt(np.mean(data.astype(np.float32) ** 2)))
                    elapsed += voice_cfg.ECHO_GATE_WINDOW_S
                    if measured < voice_cfg.ECHO_RMS_FLOOR:
                        logger.info(
                            "Reverb decayed (RMS=%.0f < %d) after %.2fs",
                            measured,
                            voice_cfg.ECHO_RMS_FLOOR,
                            elapsed,
                        )
                        return
            logger.info(
                "Reverb gate timeout after %.1fs, resuming anyway", voice_cfg.ECHO_GATE_MAX_WAIT_S
            )
        except Exception as e:
            logger.warning("RMS gate failed, falling back to fixed delay: %s", e)
            time.sleep(1.0)

    def _monitor_barge_in(self) -> bool:
        """Active mic monitor that runs while TTS is speaking. Opens its own short-lived
        capture stream (main loop has released the mic by entering _wait_for_tts), reads
        20-64ms frames, and stops TTS if RMS exceeds BARGE_IN_RMS_THRESHOLD for
        BARGE_IN_TRIGGER_FRAMES consecutive frames.

        Returns True if barge-in fired (TTS stopped by us), False if TTS ended naturally.

        Falls back to passive sleep loop on mic open failure so a flaky USB mic doesn't
        block TTS playback completion.
        """
        logger.info(
            "TTS speaking — barge-in monitor active (threshold=%d, trigger=%d × %dms blocks)",
            voice_cfg.BARGE_IN_RMS_THRESHOLD,
            voice_cfg.BARGE_IN_TRIGGER_FRAMES,
            voice_cfg.BARGE_IN_BLOCK_MS,
        )
        np = self._np
        device_rate = self._device_rate or voice_cfg.STT_RATE
        frame_size = int(device_rate * voice_cfg.BARGE_IN_BLOCK_MS / 1000)
        consecutive = 0
        max_seen = 0.0  # diagnostic: peak RMS observed during this monitor session
        try:
            if self._alsa_device is not None:
                mic_ctx = ArecordStream(
                    alsa_device=self._alsa_device,
                    rate=device_rate,
                    channels=voice_cfg.CHANNELS,
                    blocksize=frame_size,
                    np=np,
                )
            else:
                mic_ctx = self._sd.InputStream(
                    samplerate=device_rate,
                    channels=voice_cfg.CHANNELS,
                    dtype="int16",
                    blocksize=frame_size,
                    device=self._input_device,
                )
            with mic_ctx as mic:
                while self._running and self._tts_is_speaking():
                    data, overflowed = mic.read(frame_size)
                    if overflowed:
                        consecutive = 0
                        continue
                    measured = float(np.sqrt(np.mean(data.astype(np.float32) ** 2)))
                    if measured > max_seen:
                        max_seen = measured
                    if measured > voice_cfg.BARGE_IN_RMS_THRESHOLD:
                        consecutive += 1
                        if consecutive >= voice_cfg.BARGE_IN_TRIGGER_FRAMES:
                            logger.info(
                                "BARGE-IN: RMS=%.0f > %d for %d frames → stop TTS",
                                measured,
                                voice_cfg.BARGE_IN_RMS_THRESHOLD,
                                consecutive,
                            )
                            if self._tts is not None:
                                self._tts.stop()
                            return True
                    else:
                        consecutive = 0
        except Exception as e:
            logger.warning(
                "Barge-in monitor failed (%s) — falling back to passive wait", e
            )
            while self._running and self._tts_is_speaking():
                time.sleep(0.2)
        finally:
            logger.info("Barge-in monitor session end: max_rms_seen=%.0f", max_seen)
        return False

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def _loop(self):
        """Main loop: local VAD → STT on speech → disconnect on silence."""
        if hal_config.REALTIME_ENABLED:
            threading.Thread(
                target=self._realtime.start, daemon=True, name="realtime-start"
            ).start()

        time.sleep(0.5)  # Brief pause for audio subsystem to settle

        # Use arecord only when explicitly configured via HAL_AUDIO_INPUT_ALSA.
        # Auto-detection is disabled because arecord uses exclusive ALSA access,
        # which conflicts with SoundPerception's sd.rec() calls on the same device
        # (both try to open plughw:X,0 — one silently reads zeros and STT never fires).
        # Auto-detection is safe only on Pi5 where SoundPerception is not using the mic.
        # Set HAL_AUDIO_INPUT_ALSA=plughw:X,0 in .env to opt in explicitly.
        if self._alsa_device is not None:
            device_rate = voice_cfg.STT_RATE  # plughw does SRC; record directly at STT rate
            logger.info(
                "Using arecord backend (%s) at %dHz", self._alsa_device, device_rate
            )
        else:
            if self._device_rate is None:
                self._device_rate = self._detect_device_rate()
            device_rate = self._device_rate
            logger.info(
                "Using sounddevice backend (device=%s) at %dHz",
                self._input_device,
                device_rate,
            )

        frame_size = int(device_rate * voice_cfg.FRAME_DURATION_MS / 1000)
        self._device_rate = device_rate  # store for _wait_for_tts

        while self._running:
            # Wait for TTS or music to finish before opening mic
            self._wait_for_tts()
            if self._music_is_playing():
                logger.info("Music playing, pausing mic...")
                while self._running and self._music_is_playing():
                    time.sleep(0.5)
                logger.info("Music stopped, resuming mic")

            try:
                if self._alsa_device is not None:
                    mic_ctx = ArecordStream(
                        alsa_device=self._alsa_device,
                        rate=device_rate,
                        channels=voice_cfg.CHANNELS,
                        blocksize=frame_size,
                        np=self._np,
                    )
                else:
                    mic_ctx = self._sd.InputStream(
                        samplerate=device_rate,
                        channels=voice_cfg.CHANNELS,
                        dtype="int16",
                        blocksize=frame_size,
                        device=self._input_device,
                    )
                with mic_ctx as mic:
                    logger.info(
                        "Listening for speech (RMS=%d, rate=%dHz, backend=%s)...",
                        voice_cfg.RMS_THRESHOLD,
                        device_rate,
                        f"arecord({self._alsa_device})"
                        if self._alsa_device
                        else f"sd({self._input_device})",
                    )
                    self._vad_loop(mic, frame_size, device_rate)
            except Exception as e:
                logger.warning("Voice loop error: %s", e)
                if self._running:
                    time.sleep(3)

    # ------------------------------------------------------------------
    # VAD trigger loop — waits for energy + speech, then hands to STT
    # ------------------------------------------------------------------
    def _vad_loop(self, mic, frame_size: int, device_rate: int):
        """Monitor mic with local VAD, connect STT when speech detected.

        Legacy mode: returns when TTS/music starts so _loop closes the mic and
        reopens it after (incurs arecord reopen latency on the next turn).
        Warm-mic mode (HAL_WARM_MIC): never returns for TTS/music — it drains +
        discards frames while they play and resumes in place after a short
        echo-skip, keeping the arecord stream open so the next turn pays no
        reopen latency (no clipped first words after a push-to-talk cue)."""
        speech_start = None
        speech_pre_buffer = []  # frames buffered during holdoff period
        lookback = deque(maxlen=voice_cfg.PRE_ROLL_FRAMES)
        draining = False  # warm-mic: True while draining frames during TTS/music
        bc_muting = False  # True while dropping frames that carry our own cue

        # Keepalive: pre-connect STT WS so it's ready before speech is detected.
        keepalive_session = None
        last_keepalive_ping = time.time()  # throttles send_keepalive in the wait loop
        if voice_cfg.STT_KEEPALIVE:
            keepalive_session = self._stt.create_session()
            if not keepalive_session.start(lambda text, is_final: None):
                keepalive_session = None
            else:
                logger.info("STT keepalive: pre-connected, waiting for speech...")

        while self._running:
            tts_or_music = self._tts_is_speaking() or self._music_is_playing()

            # --- TTS/music active ---
            if tts_or_music:
                if not voice_cfg.WARM_MIC:
                    # Legacy: yield the mic — return so _loop closes the stream
                    # and reopens it after playback (arecord reopen latency).
                    logger.info("TTS/music started, releasing mic...")
                    if keepalive_session:
                        keepalive_session.close()
                    return
                # Warm: keep arecord OPEN, drain + discard so the speaker's audio
                # never reaches STT and the next turn pays no reopen latency.
                if not draining:
                    logger.info("TTS/music active — draining mic (warm, arecord kept open)")
                    if keepalive_session:
                        keepalive_session.close()
                        keepalive_session = None
                    speech_start = None
                    speech_pre_buffer = []
                    draining = True
                mic.read(frame_size)  # blocks ~one frame; discard. Raises if arecord dies.
                continue

            # --- Warm mic: TTS/music just ended → resume in place ---
            if draining:
                # Skip a short echo window so post-playback reverb doesn't
                # false-trigger, then resume. Bounded ≪ the 1.5s legacy reverb
                # gate so a user talking right after a cue resumes fast and the
                # pre-roll lookback (refilling below) captures their first words.
                logger.info("TTS/music ended — echo-skip then resume VAD (warm mic)")
                skip_elapsed = 0.0
                while skip_elapsed < voice_cfg.WARM_MIC_ECHO_SKIP_MAX_S and self._running:
                    d, ov = mic.read(frame_size)
                    skip_elapsed += voice_cfg.FRAME_DURATION_MS / 1000.0
                    if not ov and rms(d, self._np) < voice_cfg.ECHO_RMS_FLOOR:
                        break
                lookback.clear()
                self._silero_reset_state()
                draining = False
                if voice_cfg.STT_KEEPALIVE and self._running and not self._tts_is_speaking():
                    keepalive_session = self._stt.create_session()
                    if not keepalive_session.start(lambda text, is_final: None):
                        keepalive_session = None
                    else:
                        logger.info("STT keepalive: pre-connected, waiting for speech...")
                continue

            data, overflowed = mic.read(frame_size)
            if overflowed:
                continue

            # Re-check after blocking read — music/TTS may have started during mic.read
            if self._tts_is_speaking() or self._music_is_playing():
                if not voice_cfg.WARM_MIC:
                    return
                continue  # warm: loop back → drain branch handles it

            # Our own backchannel cue is in the room: it bypasses the TTS
            # `speaking` flag on purpose (that flag would kill the running STT
            # session), so nothing above filters it. Drop these frames entirely —
            # not just from the VAD test but from `lookback` too, or the cue
            # would come back as the next session's pre-roll. Any speech run in
            # progress is abandoned: a cue only fires after the partial stalled,
            # so there is no user utterance to clip here.
            if self._backchannel.self_audio_active:
                if not bc_muting:
                    bc_muting = True
                    logger.info("Backchannel cue in the room — VAD muted until it decays")
                if speech_start is not None:
                    speech_start = None
                    speech_pre_buffer = []
                continue
            if bc_muting:
                # Same cleanup the warm-mic drain does on resume: the dropped
                # frames are a discontinuity for Silero's LSTM, and the lookback
                # must not pre-roll audio from before the cue.
                bc_muting = False
                lookback.clear()
                self._silero_reset_state()
                logger.info("Backchannel cue decayed — VAD resumed")

            # Append to lookback for pre-roll.
            lookback.append(data)

            # Keep the pre-connected STT WS warm: ping every STT_KEEPALIVE_PING_S
            # while idle (speech not started) so the server doesn't idle-close it
            # and force a slow cold-reconnect at speech start (→ empty transcript).
            if (
                keepalive_session is not None
                and speech_start is None
                and (time.time() - last_keepalive_ping) >= voice_cfg.STT_KEEPALIVE_PING_S
                and hasattr(keepalive_session, "send_keepalive")
            ):
                keepalive_session.send_keepalive()
                last_keepalive_ping = time.time()

            energy = rms(data, self._np)
            self._mic_level = energy
            self._mic_level_ts = time.time()

            if energy >= voice_cfg.RMS_THRESHOLD and self._webrtcvad_is_speech(data, device_rate):
                if speech_start is None:
                    speech_start = time.time()
                    speech_pre_buffer = [data]
                else:
                    speech_pre_buffer.append(data)
                # Wait for holdoff before connecting STT (avoid short noises)
                if (time.time() - speech_start) >= voice_cfg.SPEECH_HOLDOFF_S:
                    # Run Silero on accumulated buffer (needs multiple chunks for LSTM)
                    if self._silero_vad is not None:
                        combined = self._np.concatenate(speech_pre_buffer)
                        if not self._silero_is_speech(combined, device_rate):
                            speech_start = None
                            speech_pre_buffer = []
                            continue
                    # Prepend pre-trigger history from lookback.
                    buffered = len(speech_pre_buffer)
                    history = (
                        list(lookback)[:-buffered] if buffered > 0 else list(lookback)
                    )
                    all_frames = history + speech_pre_buffer
                    logger.info(
                        "Speech detected (RMS=%.0f) — pre-roll=%d frames (~%dms) + holdoff=%d frames",
                        energy,
                        len(history),
                        len(history) * voice_cfg.FRAME_DURATION_MS,
                        buffered,
                    )
                    # Speech is confirmed (Silero has agreed) — the moment to
                    # ask whether the user had turned toward the lamp just
                    # before saying it. Reads the gaze buffer BACKWARDS; it does
                    # not capture anything now, because the turn happened before
                    # this line ran. No-op unless the feature is armed.
                    try:
                        from hal.drivers.tracking import gaze

                        gaze.on_speech_start()
                    except Exception as e:
                        logger.debug("gaze wake check skipped: %s", e)
                    speech_pre_buffer = [
                        resample_to_stt(f, device_rate, voice_cfg.STT_RATE, self._np)
                        for f in all_frames
                    ]
                    self._stream_session(
                        mic,
                        frame_size,
                        device_rate,
                        preconnected_session=keepalive_session,
                        speech_pre_buffer=speech_pre_buffer,
                    )
                    keepalive_session = None
                    speech_start = None
                    speech_pre_buffer = []
                    # Clear lookback so the next session doesn't replay tail
                    lookback.clear()
                    self._silero_reset_state()
                    logger.info("VAD resumed — mic active, waiting for next speech")
                    # Cooldown after session to let resources clean up
                    time.sleep(voice_cfg.SESSION_COOLDOWN_S)
                    # Pre-connect next session immediately
                    if voice_cfg.STT_KEEPALIVE and self._running and not self._tts_is_speaking():
                        keepalive_session = self._stt.create_session()
                        if not keepalive_session.start(lambda text, is_final: None):
                            keepalive_session = None
                        else:
                            logger.info(
                                "STT keepalive: pre-connected, waiting for speech..."
                            )
            else:
                speech_start = None
                speech_pre_buffer = []
                if energy >= voice_cfg.RMS_THRESHOLD:
                    logger.debug(
                        "VAD: RMS=%.0f above threshold but Silero rejected — not speech",
                        energy,
                    )

    # ------------------------------------------------------------------
    # STT streaming session — fires while user is speaking
    # ------------------------------------------------------------------
    def _stream_session(
        self,
        mic,
        frame_size: int,
        device_rate: int,
        preconnected_session=None,
        speech_pre_buffer=None,
    ):
        """Stream audio to STT provider until silence or TTS interrupts.

        Buffer lifecycle (one per call):
            START  — ``audio_buffer = []`` created as a local variable
            FILL   — every frame that goes to STT is also appended here
            USE    — at session end the finally block reads it for speaker ID + SER
            END    — function returns → local ``audio_buffer`` goes out of
                     scope → garbage-collected. NO state leaks to the next
                     ``_stream_session`` call.
        """
        # A keepalive session pre-connected on the previous turn can go STALE if
        # the user stayed silent past the STT provider's inactivity window (~10s):
        # the upstream closes the idle WS (code 1000) and the next send() raises
        # ConnectionClosed → the whole turn's STT is lost. Detect the dead session
        # up front and fall through to a fresh connect instead of reusing it.
        if preconnected_session is not None and preconnected_session.is_closed():
            logger.warning(
                "STT keepalive: pre-connected session went stale (idle close) — "
                "connecting a fresh session for this turn"
            )
            try:
                preconnected_session.close()
            except Exception:
                pass
            preconnected_session = None

        stt_session = preconnected_session or self._stt.create_session()
        # Latch focus at session start. A user who began speaking before the
        # deadline may finish their sentence after it, but a later session must
        # use the wake phrase again.
        wakeword_followup_active = (
            hal_config.WAKEWORD_ENABLED and self._wakeword_focus.is_active()
        )
        if wakeword_followup_active:
            logger.info("Wake-word follow-up focus accepted for this session")

        last_partial = [""]
        final_segments = []
        final_sent = [False]
        # The listening cue fires on the FIRST STT PARTIAL — never at session
        # open. A partial is proof a human said words; the entry VAD is not.
        # That VAD is tuned wide open on purpose so quiet speech is never
        # missed, and the price is that most sessions it opens are noise
        # (measured on a lamp 2026-07-30: 28 of 31 ended with an empty
        # transcript). There used to be an earlier LED-only stage at session
        # open, justified as "instant feedback, cheap to be wrong" — it was
        # wrong ~90% of the time, and once the strip's resting look went dark
        # a wrong cue stopped being cheap: it became the most visible thing on
        # the device. Cost of waiting for the partial: 1.5-2.5s (measured).
        listening_emotion_sent = [False]
        # Collect every resampled 16kHz int16 PCM chunk so we can identify the
        # speaker at session end. This list is LOCAL to _stream_session — a
        # fresh empty list every call, no cross-session carry-over.
        audio_buffer: list[bytes] = []
        # Index of the last frame with speech energy — bound HERE (not only in
        # the streaming loop below) so the finally → finalize_session path is
        # safe even when an exception fires during connect or pre-flush, before
        # the streaming loop runs (e.g. a stale keepalive WS raising on send).
        # -1 = no speech seen → finalize_session skips the trailing-silence trim.
        last_speech_idx: int = -1
        pre_frames_from_vad = len(speech_pre_buffer or [])
        logger.info(
            "Session START — pre_from_vad=%d frames, device_rate=%dHz",
            pre_frames_from_vad,
            device_rate,
        )
        # A partial match is provisional: STT can correct a name in its final
        # result ("Moon" → "Mom"). It improves observability while the user is
        # speaking, but a turn is not dispatched or committed to realtime until
        # a final result confirms the wake phrase.
        wake_word_detected = threading.Event()
        wake_word_confirmed = threading.Event()
        capture_complete = threading.Event()
        # STT providers disagree on interim updates: some re-send the entire
        # hypothesis ("Hello" → "Hello Luna"), while others emit only the new
        # token ("Hello" → "Luna"). Retain a leading transcript hypothesis so
        # either shape can arm the gate as soon as the alias is complete.
        wake_partial_hypothesis = [""]
        wake_final_hypothesis = [""]

        def wake_partial_candidate(text: str) -> str:
            wake_partial_hypothesis[0] = merge_stt_hypothesis(
                wake_partial_hypothesis[0], text
            )
            return wake_partial_hypothesis[0]

        def wake_final_candidate(text: str) -> str:
            # Do not merge an interim hypothesis into the final one. That would
            # preserve a corrected false-positive wake word indefinitely.
            wake_final_hypothesis[0] = merge_stt_hypothesis(
                wake_final_hypothesis[0], text
            )
            wake_partial_hypothesis[0] = ""
            return wake_final_hypothesis[0]

        def addressed_to_us() -> bool:
            """Whether the sentence being spoken has been shown to be for us.

            True when no wake word is configured (every utterance is), or when
            one was heard, or inside the follow-up window a wake word, a click
            or a gaze opened. Everything that CLAIMS to be the addressee — the
            listening cue, the backchannel — has to ask this first, or the lamp
            answers conversations it was never part of.
            """
            if not hal_config.WAKEWORD_ENABLED:
                return True
            return wake_word_detected.is_set() or wakeword_followup_active

        def fire_listening_cue() -> None:
            """Show the listening cue, once per session, only when this turn is
            actually addressed to the device.

            The cue is not free: it paints the strip AND holds the body still
            (a preset with servo=None halts the animation loop — see
            routes/emotion.py). With a wake word configured, firing it on any
            partial means every conversation happening in the room lights the
            lamp up and freezes it, which reads as the device butting in.
            So: wake word heard, or an open follow-up window. Without a wake
            word configured every utterance IS addressed to the device, so the
            cue fires on the first partial as before.
            """
            if listening_emotion_sent[0]:
                return
            if not addressed_to_us():
                return
            listening_emotion_sent[0] = True
            self._set_emotion_local(presets.EMO_LISTENING)

        def open_wake_word_gate(candidate: str, source: str) -> None:
            if (
                hal_config.WAKEWORD_ENABLED
                and candidate
                and self._decorator.starts_with_wake_word(candidate)
                and not wake_word_detected.is_set()
            ):
                wake_word_detected.set()
                logger.info(
                    "Wake-word gate opened by STT %s: '%s'", source, candidate
                )
                # Fire here too, not just from the partial below: the gate can
                # open on a later partial (or on the final), and the cue should
                # land the moment the device knows it is being addressed.
                fire_listening_cue()

        def confirm_wake_word_gate(candidate: str) -> None:
            if (
                hal_config.WAKEWORD_ENABLED
                and candidate
                and self._decorator.starts_with_wake_word(candidate)
            ):
                wake_word_confirmed.set()
                open_wake_word_gate(candidate, "final")
                logger.info("Wake-word gate confirmed by STT final: '%s'", candidate)

        def on_transcript(text: str, is_final: bool):
            if text.strip():
                self._last_transcript_ts = time.time()
            if not is_final:
                logger.info("STT partial: '%s'", text)
                candidate = wake_partial_candidate(text)
                if hal_config.WAKEWORD_ENABLED:
                    logger.debug("Wake-word partial candidate: '%s'", candidate)
                    open_wake_word_gate(candidate, "partial")
                last_partial[0] = text
                # Same gate as the listening cue below. A backchannel is the
                # device saying "go on, I'm listening", which is a claim to be
                # the addressee — so it must not fire for a sentence the device
                # has not been shown is meant for it. It predates the wake gate
                # (Apr 2026, "call on every STT partial") and kept firing on
                # every utterance after that gate arrived, so the lamp murmured
                # "Right" at conversations between two other people, and made
                # testing the openers actively misleading: the cue sounds like
                # acknowledgement while the turn is dropped unheard.
                if addressed_to_us():
                    self._backchannel.on_partial(text)
                fire_listening_cue()
                return
            # Accumulate final segments — don't send yet, wait for session close.
            # Flux model fires multiple EndOfTurn events for natural pauses within
            # one utterance, so sending immediately would split a single sentence.
            logger.info("STT final segment: '%s'", text)
            if hal_config.WAKEWORD_ENABLED:
                confirm_wake_word_gate(wake_final_candidate(text))
            # A turn's text is the LATEST sentence: default to this final, even
            # if shorter than the partial. Only when the final is BOTH shorter AND
            # too dissimilar (case-insensitive ratio < _TRANSCRIPT_MIN_SIMILARITY)
            # is it a NEW turn → keep prev as its own segment. Appended text keeps
            # original casing.
            prev = last_partial[0]
            if (
                prev
                and len(text) < len(prev)
                and SequenceMatcher(None, prev.lower(), text.lower()).ratio()
                < _TRANSCRIPT_MIN_SIMILARITY
            ):
                # New turn: keep the previous text and this final as separate segments.
                segments = [prev, text]
            else:
                # Same turn: a correction (shorter but similar) or a longer/first
                # final → this final IS the turn's latest text.
                segments = [text]
            for seg in segments:
                if seg:
                    final_segments.append(seg)
            last_partial[0] = ""
            final_sent[0] = True

        rt_audio_buffer: list = []
        # A noise-drop can be rebuilding a clean Gemini session in the
        # background. Keep this entire capture local in that narrow window so
        # no frame is sent to the old, about-to-be-discarded activity.
        realtime_deferred = False
        realtime_turn_started = False
        realtime_start_failed = False
        # Voice speaker-ID for THIS turn — resolved once after capture (below) and
        # passed into build_turn_context() so the realtime reply names the actual
        # speaker. None until resolved / on unknown → face fallback.
        turn_identity = None  # (final_msg, se_user, display) or None
        turn_speaker_display = None
        # Which speaker name actually WENT OUT in this turn's [TURN CONTEXT].
        # In always-listening mode the context is sent when the session opens —
        # before a single audio frame exists — so the prepass below cannot have
        # run yet and this is the face-derived user (or None). Compared against
        # the prepass result to decide whether a late correction is needed.
        sent_turn_speaker = None
        turn_context_sent = False

        def start_realtime_turn() -> bool:
            """Open realtime as soon as the wake-word partial is available.

            Only this capture thread touches the realtime session. When it sees
            the Event set by the STT callback, it flushes all retained audio
            once, including the opening wake phrase, then later frames stream
            straight through as in always-listening mode.
            """
            nonlocal realtime_deferred, realtime_turn_started, realtime_start_failed
            nonlocal sent_turn_speaker, turn_context_sent
            if realtime_turn_started or realtime_start_failed:
                return False

            if hal_config.WAKEWORD_ENABLED:
                # Preserve the always-listening path below exactly. Wake-word
                # turns defer all realtime I/O until capture is complete and a
                # FINAL STT result confirms the phrase, so a rebuild cannot split
                # one utterance across old/new sessions.
                if (
                    not capture_complete.is_set()
                    or not (wake_word_confirmed.is_set() or wakeword_followup_active)
                    or not hal_config.REALTIME_ENABLED
                ):
                    return False
                if self._realtime.rebuilding or not self._realtime.available:
                    logger.info(
                        "[realtime] Wake-word turn falls back — session unavailable after final confirmation"
                    )
                    return False
                try:
                    self._realtime.send_text(build_turn_context(turn_speaker_display))
                    sent_turn_speaker = turn_speaker_display
                    turn_context_sent = True
                    for audio_f32 in rt_audio_buffer:
                        self._realtime.append_audio(audio_f32)
                    realtime_turn_started = True
                    logger.info(
                        "[realtime] Wake-word/follow-up authorized; flushed %d buffered frame(s)",
                        len(rt_audio_buffer),
                    )
                    return True
                except Exception as e:
                    realtime_start_failed = True
                    logger.warning(
                        "[realtime] Wake-word start failed; forwarding final STT to main agent: %s",
                        e,
                    )
                    return False

            realtime_turn_started = True
            if not hal_config.REALTIME_ENABLED:
                return True

            self._realtime.prepare_turn()
            realtime_deferred = self._realtime.rebuilding
            if not realtime_deferred and self._realtime.available:
                try:
                    # ALWAYS-LISTENING PATH. This fires at session open, so
                    # turn_speaker_display is still None here by construction —
                    # the voiceprint needs the completed capture. The speaker-ID
                    # prepass in the finally block sends a correction afterwards.
                    self._realtime.send_text(build_turn_context(turn_speaker_display))
                    sent_turn_speaker = turn_speaker_display
                    turn_context_sent = True
                    for audio_f32 in rt_audio_buffer:
                        self._realtime.append_audio(audio_f32)
                    if hal_config.WAKEWORD_ENABLED:
                        logger.info(
                            "[realtime] Wake-word gate opened; flushed %d buffered frame(s)",
                            len(rt_audio_buffer),
                        )
                except Exception as e:
                    logger.warning("[realtime] start turn failed: %s", e)
                    rt_audio_buffer.clear()
            return True
        try:
            if preconnected_session:
                # Already connected — swap in the real transcript callback.
                stt_session._on_transcript_cb = on_transcript
                logger.info("STT keepalive: reusing pre-connected session")

            connect_ok = [False]
            connect_done = threading.Event()

            def _do_connect():
                connect_ok[0] = stt_session.start(on_transcript)
                connect_done.set()

            if preconnected_session:
                connect_ok[0] = True
                connect_done.set()
            else:
                threading.Thread(
                    target=_do_connect, daemon=True, name="stt-connect"
                ).start()

            pre_buffer = []
            while not connect_done.wait(timeout=0.005):
                if self._tts_is_speaking():
                    connect_done.wait(timeout=2)
                    break
                data, overflowed = mic.read(frame_size)
                if not overflowed:
                    pre_buffer.append(
                        resample_to_stt(data, device_rate, voice_cfg.STT_RATE, self._np)
                    )

            if not connect_ok[0]:
                return

            # Always-listening mode starts now. In wake-word mode this returns
            # immediately until an STT partial callback sets the Event.
            start_realtime_turn()

            # Flush holdoff audio (frames captured before STT connect, both paths)
            all_pre = (speech_pre_buffer or []) + pre_buffer
            if all_pre:
                logger.info(
                    "Session FILL (pre-flush) — added %d frames (~%.0fms) to buffer",
                    len(all_pre),
                    len(all_pre) * voice_cfg.FRAME_DURATION_MS,
                )
                # A keepalive socket can pass the earlier is_closed() check then
                # receive a peer close just as speech starts. Retry the COMPLETE
                # pre-roll on a fresh session before recording/forwarding it so
                # the user does not lose the opening words (or get duplicate
                # frames in realtime) because the old socket accepted only a
                # prefix before its close.
                used_preconnected = preconnected_session is not None

                def _send_pre_roll():
                    if stt_session.is_closed():
                        raise RuntimeError("pre-connected STT session is closed")
                    for pre_frame in all_pre:
                        stt_session.send_audio(pre_frame)

                try:
                    _send_pre_roll()
                except Exception as e:
                    if not used_preconnected:
                        raise
                    logger.warning(
                        "STT keepalive closed at speech start (%s) — reconnecting "
                        "and replaying %d pre-roll frame(s)",
                        "normal 1000 close" if _is_normal_ws_close(e) else str(e),
                        len(all_pre),
                    )
                    try:
                        stt_session.close()
                    except Exception:
                        pass
                    stt_session = self._stt.create_session()
                    if not stt_session.start(on_transcript):
                        raise RuntimeError("fresh STT session failed to connect") from e
                    _send_pre_roll()

                # The full pre-roll reached one live STT session. Only now make
                # it part of local/realtime buffers, so a failed keepalive retry
                # cannot duplicate or retain audio sent to a dead socket.
                for frame in all_pre:
                    audio_buffer.append(frame)
                    # Keep the full realtime copy even while a noise-drop rebuild
                    # is warming. It is flushed once to the replacement session
                    # at turn end, preserving the opening words.
                    if hal_config.REALTIME_ENABLED:
                        audio_f32 = pcm16_bytes_to_float32(frame)
                        audio_f32 = resample_float32(
                            audio_f32, voice_cfg.STT_RATE, self._realtime.sample_rate
                        )
                        rt_audio_buffer.append(audio_f32)
                        if (
                            realtime_turn_started
                            and not realtime_deferred
                            and self._realtime.available
                        ):
                            self._realtime.append_audio(audio_f32)

                # A partial can arrive while the STT pre-roll is sent. Open the
                # gate now and flush that complete pre-roll exactly once.
                start_realtime_turn()

            self._listening = True
            last_speech_time = time.time()
            session_start = time.time()
            # Track index of last frame with speech energy — used to trim
            # trailing silence from the speaker-recognition buffer at session
            # end. SILENCE_TIMEOUT_S holds the session open for ~2.5s after
            # the user stops, so without this the voiceprint ends up 30-50%
            # silence and the embedding degrades. (Pre-initialized to -1 above.)
            last_speech_idx = len(audio_buffer) - 1
            # Above-RMS frames waiting for Silero to confirm they are speech
            # before they refresh the silence clock. See SILENCE_VAD_ENABLED.
            silence_probe: list = []
            silence_vad_on = (
                voice_cfg.SILENCE_VAD_ENABLED
                and voice_cfg.SILENCE_VAD_WINDOW_FRAMES > 0
            )
            if silence_vad_on and self._silence_vad is not None:
                self._silence_vad.reset_state()
            noise_windows = 0
            # No LED here: the cue waits for the first STT partial (see the
            # listening-cue note above). Opening a session is cheap to be wrong
            # about; lighting the strip is not.
            #
            # Tell the OS server a mic session is open. NOT an LED signal
            # despite the event name — the handler only extends a window that
            # suppresses passive sensing (motion/presence) so it can't steal
            # the turn while the user is speaking. Firing it on a noise session
            # is harmless, so it stays ungated.
            try:
                requests.post(
                    "http://127.0.0.1:5000/api/sensing/event",
                    json={"type": "voice_listening", "message": "listening"},
                    timeout=0.3,
                )
            except Exception:
                pass

            while self._running and not stt_session.is_closed():
                # Only the capture thread opens and flushes the realtime activity;
                # The STT callback merely latches wake_word_detected.
                start_realtime_turn()
                # If TTS or music starts mid-session, stop streaming immediately
                if self._tts_is_speaking():
                    logger.info("TTS started mid-session, closing STT to avoid echo")
                    break
                if self._music_is_playing():
                    logger.info("Music started mid-session, closing STT")
                    break

                # Guard against zombie sessions
                if (time.time() - session_start) > voice_cfg.MAX_SESSION_DURATION_S:
                    logger.warning(
                        "STT session exceeded %ds, force-closing",
                        voice_cfg.MAX_SESSION_DURATION_S,
                    )
                    break

                data, overflowed = mic.read(frame_size)
                if overflowed:
                    continue

                resampled = resample_to_stt(data, device_rate, voice_cfg.STT_RATE, self._np)
                try:
                    stt_session.send_audio(resampled)
                except Exception as e:
                    logger.warning("send_audio failed (connection dead?): %s", e)
                    break
                audio_buffer.append(resampled)

                # Parallel: stream to realtime model (non-blocking queue put).
                # During a pending noise-drop rebuild retain frames locally and
                # flush them once to the clean replacement session below.
                if hal_config.REALTIME_ENABLED:
                    audio_f32 = pcm16_bytes_to_float32(resampled)
                    audio_f32 = resample_float32(
                        audio_f32, voice_cfg.STT_RATE, self._realtime.sample_rate
                    )
                    rt_audio_buffer.append(audio_f32)
                    opened_now = start_realtime_turn()
                    if (
                        realtime_turn_started
                        and not opened_now
                        and not realtime_deferred
                        and self._realtime.available
                    ):
                        self._realtime.append_audio(audio_f32)

                energy = rms(data, self._np)
                self._mic_level = energy
                self._mic_level_ts = time.time()
                if energy >= voice_cfg.RMS_THRESHOLD:
                    if not silence_vad_on:
                        last_speech_time = time.time()
                        last_speech_idx = len(audio_buffer) - 1
                    else:
                        # RMS said "loud". Ask Silero whether it was a VOICE
                        # before letting it hold the session open. Batched: one
                        # inference per window, not per frame.
                        silence_probe.append(data)
                        if len(silence_probe) >= voice_cfg.SILENCE_VAD_WINDOW_FRAMES:
                            window = self._np.concatenate(silence_probe)
                            probe_frames = len(silence_probe)
                            silence_probe = []
                            if self._silence_window_is_speech(window, device_rate):
                                last_speech_time = time.time()
                                last_speech_idx = len(audio_buffer) - 1
                            else:
                                # Not a voice — leave the clock running so a
                                # noisy room can still time out. The frames stay
                                # in audio_buffer; only the clock is withheld.
                                noise_windows += 1
                                if noise_windows in (1, 10, 50):
                                    logger.info(
                                        "Silence clock: %d loud window(s) rejected as "
                                        "non-speech (%d frames each)",
                                        noise_windows, probe_frames,
                                    )
                elif (time.time() - last_speech_time) > voice_cfg.SILENCE_TIMEOUT_S:
                    if noise_windows:
                        logger.info(
                            "Silence detected, disconnecting STT "
                            "(%d loud window(s) were non-speech)", noise_windows
                        )
                    else:
                        logger.info("Silence detected, disconnecting STT")
                    break
        except Exception as e:
            if _is_normal_ws_close(e):
                logger.warning(
                    "STT session closed normally (1000) before turn completion; "
                    "audio for this turn was discarded"
                )
            else:
                logger.error("STT stream error: %s", e)
        finally:
            self._backchannel.reset()
            self._listening = False
            stt_session.close()
            combined, ser_audio_buffer, buf_duration = finalize_session(
                audio_buffer, last_partial, final_segments, last_speech_idx
            )
            capture_complete.set()
            if (
                hal_config.WAKEWORD_ENABLED
                and wake_word_detected.is_set()
                and not wake_word_confirmed.is_set()
            ):
                # Last look, on the ASSEMBLED transcript. The per-segment checks
                # above run on wake_final_candidate(), which passes through
                # merge_stt_hypothesis() — and that keeps only \w+ tokens, so
                # sentence punctuation is gone by then. A wake phrase opening a
                # LATER sentence ("Is that match playing tonight? Hello lamp,
                # let's check it out.") therefore looked mid-sentence and the
                # whole turn was dropped, wake word and all (device-observed
                # 18/08/2026). `combined` is the real transcript with its
                # punctuation intact, which is what the sentence rule needs.
                if self._decorator.starts_with_wake_word(combined):
                    wake_word_confirmed.set()
                    logger.info(
                        "Wake-word confirmed on assembled transcript: %r", combined
                    )
                else:
                    logger.info(
                        "Wake-word partial rejected — no matching final STT result; dropping turn"
                    )

            # Noise guard for empty-STT turns: a session can open on a noise blip
            # that fools the entry VAD, then STT finds no words. Re-check the FULL
            # captured buffer with Silero; if it isn't speech, run_realtime_turn
            # treats it as noise and skips the commit (no self-talk, no wasted
            # tokens). Fail-open: any error → leave it True (don't drop a real turn).
            rt_audio_is_speech = True
            if (
                not combined
                and hal_config.REALTIME_REQUIRE_SPEECH_ON_EMPTY_STT
                and audio_buffer
            ):
                try:
                    pcm = self._np.frombuffer(
                        b"".join(audio_buffer), dtype=self._np.int16
                    )
                    rt_audio_is_speech = self._rt_noise_is_speech(pcm)
                    logger.info(
                        "[realtime] noise-guard ran: empty STT, silero_speech=%s "
                        "(samples=%d, dur=%.2fs)",
                        rt_audio_is_speech, len(pcm), buf_duration,
                    )
                except Exception as e:
                    logger.warning("Realtime noise-guard buffer decode failed: %s", e)

            # Speaker-ID prepass: resolve the voice speaker ONCE now that capture
            # is complete — the voiceprint needs the whole utterance, so this is
            # the EARLIEST point it can run, and in always-listening mode it is
            # already later than the [TURN CONTEXT] send (hence the correction
            # below). The result is reused by dispatch_turn (recognition never
            # runs twice). Unknown / gate-reject → display None → face fallback. Wrapped
            # defensively: it now runs on the reply path, so a recognizer error
            # must not kill the turn.
            if combined:
                _final_text, _ = self._decorator.classify_wake_word(combined)
                try:
                    turn_identity = self._decorator.identify_and_decorate(
                        _final_text, audio_buffer
                    )
                    turn_speaker_display = turn_identity[2]
                    # Promote a confident match to the device-wide voice
                    # identity, so it outlives this turn the way a face does.
                    # display (index 2) is set ONLY on a confident match, so it
                    # gates the write: unknown / gate-reject / server error all
                    # leave the previous value to age out on its own rather
                    # than replacing it with a guess. The stored label is the
                    # NORMALIZED name (index 1), matching what face reports.
                    if turn_speaker_display and turn_identity[1]:
                        from hal import app_state as _identity_state

                        _identity_state.set_voice_user(
                            turn_identity[1], turn_speaker_display
                        )
                except Exception as e:
                    logger.warning("[realtime] speaker-ID prepass failed: %s", e)
                logger.info(
                    "[realtime] speaker-ID prepass: display=%r (se_user=%r) — "
                    "context already sent with speaker=%r → %s",
                    turn_speaker_display,
                    turn_identity[1] if turn_identity else None,
                    sent_turn_speaker,
                    "correction needed"
                    if (turn_speaker_display and turn_speaker_display != sent_turn_speaker)
                    else "no correction needed",
                )

            # Capture can end just after the STT callback. One final check
            # avoids dropping a matched partial that raced the loop exit.
            #
            # Guarded by the same noise check as the deferred flush below: this
            # call site runs AFTER the noise guard, so an empty non-speech turn
            # is already known to be uncommittable here. Opening the turn anyway
            # sent [TURN CONTEXT] plus the whole audio buffer into the model's
            # open activity — billed, then thrown away one line later by the
            # skip-commit path, which also had to swap in a fresh session.
            # Sessions opened earlier in the capture (always-listening path) have
            # already streamed audio, so they still run and still get discarded.
            if is_noise_turn(combined, buf_duration, rt_audio_is_speech):
                if not realtime_turn_started:
                    logger.info(
                        "[realtime] Noise turn — not opening realtime turn after capture "
                        "(empty STT, silero_speech=%s, dur=%.2fs); nothing sent to model",
                        rt_audio_is_speech,
                        buf_duration,
                    )
            else:
                start_realtime_turn()

            # `discard_open_activity()` starts its replacement session in the
            # background. A user can begin the next utterance before that
            # handshake completes; in that case we retained every frame above.
            # Wait only after capture has ended, then inject context and flush
            # the complete ordered audio once. A slow/failed reconnect leaves
            # the realtime buffer uncommitted so the normal OS-server fallback
            # still receives the STT transcript without a missing opening word.
            if (
                realtime_turn_started
                and realtime_deferred
                and rt_audio_buffer
                and not is_noise_turn(combined, buf_duration, rt_audio_is_speech)
            ):
                if self._realtime.wait_until_available():
                    try:
                        self._realtime.send_text(build_turn_context(turn_speaker_display))
                        sent_turn_speaker = turn_speaker_display
                        turn_context_sent = True
                        for audio_f32 in rt_audio_buffer:
                            self._realtime.append_audio(audio_f32)
                        logger.info(
                            "[realtime] Flushed %d buffered frame(s) after noise-drop rebuild",
                            len(rt_audio_buffer),
                        )
                    except Exception as e:
                        # Do not commit a partial deferred turn. No audio was
                        # intended for the old activity; falling back preserves
                        # the transcript and avoids contaminating the new session.
                        logger.warning(
                            "[realtime] deferred audio flush failed; falling back: %s", e
                        )
                        rt_audio_buffer.clear()
                else:
                    logger.warning(
                        "[realtime] noise-drop rebuild not ready after capture; "
                        "falling back to main agent"
                    )

            # --- Realtime voice agent (speaks the reply for this turn) ---------
            # Wake-word mode only commits a turn authorized by a final wake
            # phrase or the short follow-up focus window.
            # Late identity correction. The always-listening path sends this
            # turn's [TURN CONTEXT] at session open, when no audio exists yet and
            # the voice speaker is therefore unknowable — so the prepass result
            # above never reached the model and the reply named the face-derived
            # user (or whoever session memory held). Send the correction now,
            # still BEFORE run_realtime_turn commits the audio, so it is part of
            # this turn. Skipped when the context already carried the right name
            # (wake-word / deferred paths, which send after the prepass).
            if (
                realtime_turn_started
                and turn_context_sent
                and turn_speaker_display
                and turn_speaker_display != sent_turn_speaker
                and hal_config.REALTIME_ENABLED
                and self._realtime.available
                and not is_noise_turn(combined, buf_duration, rt_audio_is_speech)
            ):
                try:
                    self._realtime.send_text(
                        build_speaker_correction(turn_speaker_display)
                    )
                    logger.info(
                        "[realtime] speaker correction sent: context went out with "
                        "%r, voice ID resolved %r",
                        sent_turn_speaker,
                        turn_speaker_display,
                    )
                except Exception as e:
                    logger.warning("[realtime] speaker correction send failed: %s", e)

            if realtime_turn_started:
                rt = run_realtime_turn(
                    self._realtime,
                    self._tts,
                    self.strip_rt_markers,
                    combined,
                    rt_audio_buffer,
                    buf_duration,
                    rt_audio_is_speech,
                )
            else:
                rt = RealtimeTurnResult()

            # --- OS server send + SER (reuses the prepass speaker-ID) ----------
            # Re-check the focus instead of trusting only the session-start
            # latch: a button click can open the window while this session is
            # already streaming, and that click means the floor is the user's
            # for the sentence they are saying right now. The latch still wins
            # on its own (a session that started inside the window stays
            # authorized even if the deadline lapses mid-sentence), so this
            # only ever adds authorization.
            wakeword_followup_active = (
                wakeword_followup_active
                or (hal_config.WAKEWORD_ENABLED and self._wakeword_focus.is_active())
            )
            wakeword_authorized = wake_word_confirmed.is_set() or wakeword_followup_active
            if should_dispatch_to_main(
                hal_config.WAKEWORD_ENABLED,
                wakeword_authorized,
            ):
                # A realtime connection failure or silent timeout is not a
                # handled turn. Preserve the STT fallback so a wake-word command
                # never disappears just because Gemini is temporarily down.
                dispatch_turn(
                    self._decorator,
                    self._sensing_sender,
                    combined,
                    audio_buffer,
                    ser_audio_buffer,
                    rt,
                    event_type_override=(
                        "voice_followup"
                        if wakeword_followup_active and not wake_word_confirmed.is_set()
                        else None
                    ),
                    identity=turn_identity,
                )
                if combined and hal_config.WAKEWORD_ENABLED and wakeword_authorized:
                    if self._wakeword_focus.refresh():
                        logger.info(
                            "Wake-word follow-up focus refreshed for %.0fs",
                            hal_config.WAKEWORD_FOLLOWUP_TIMEOUT_S,
                        )
            else:
                self._decorator.submit_speech_emotion_from_session(ser_audio_buffer)
                # A rejected utterance deliberately has no downstream agent to
                # replace the listening cue with thinking or TTS. Restore the
                # prior resting LED immediately rather than setting EMO_IDLE:
                # idle is a persistent amber effect, not a cleanup state. Do
                # not do this for an armed realtime turn: that path may already
                # be expressing an emotion while speaking its direct reply.
                if (
                    hal_config.WAKEWORD_ENABLED
                    and not wake_word_confirmed.is_set()
                    and listening_emotion_sent[0]
                ):
                    from hal import app_state

                    app_state.clear_listening_cue()

            # Close the sensing-suppression window (see the matching
            # voice_listening post above — neither event drives an LED).
            try:
                requests.post(
                    "http://127.0.0.1:5000/api/sensing/event",
                    json={"type": "voice_listening_end", "message": "done"},
                    timeout=0.3,
                )
            except Exception:
                pass

            # No cue cleanup for a noise session: nothing was painted, because
            # the cue only fires once a partial proves someone spoke. A session
            # that ends with an empty transcript leaves the strip untouched.

            # Safety net: if we fired emotion=listening but no follow-up
            # emotion arrives (LLM error, silence-only after first partial,
            # TTS interrupt before response), blue-pulse would hang. After
            # 8s, reset to idle — but only if current emotion is still
            # "listening" so we don't stomp on a real LLM-driven emotion.
            if listening_emotion_sent[0]:

                def _reset_if_still_listening():
                    try:
                        from hal import app_state

                        app_state.clear_listening_cue()
                    except Exception as e:
                        logger.warning("listening idle-reset failed: %s", e)

                threading.Timer(8.0, _reset_if_still_listening).start()

            # Buffer is a local variable — once this function returns it is
            # garbage-collected. The next _stream_session call starts with a
            # fresh empty buffer. Leaving this log here as a breadcrumb so
            # operators can confirm session boundaries in the log stream.
            logger.info("Session RESET — audio_buffer discarded, ready for next turn")
