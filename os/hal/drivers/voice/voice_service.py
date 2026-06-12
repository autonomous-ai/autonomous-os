"""
Voice Service — local VAD + pluggable STT for autonomous sensing.

Pipeline:
  1. Mic always on, local RMS energy check (free, zero cost)
  2. Speech detected → create STT session, stream audio
  3. Silence for SILENCE_TIMEOUT → close session (stop billing)
  4. Transcripts → POST to Lamp Server /api/sensing/event
  5. Lamp Go → local intent match or OpenClaw → AI responds → POST /voice/speak

STT provider is pluggable (default: Deepgram).

Helpers live in `_internal/` — config constants, audio I/O, VAD filters,
speaker decoration, and Lamp event sender.
"""

import logging
import re
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from typing import Optional

import requests

from hal import app_state as hal_app_state
from hal import config as hal_config
from hal.drivers.realtime.models import TextOutput as RTTextOutput
from hal.drivers.realtime.orchestrator import DelegateSignal, RealtimeOrchestrator
from hal.drivers.realtime.utils import pcm16_bytes_to_float32, resample_float32
from hal.drivers.voice._internal.audio_dsp import resample_to_stt, rms
from hal.drivers.voice._internal.audio_recorder import ArecordStream
from hal.drivers.voice._internal.config import (
    BARGE_IN_BLOCK_MS,
    BARGE_IN_ENABLED,
    BARGE_IN_RMS_THRESHOLD,
    BARGE_IN_TRIGGER_FRAMES,
    CHANNELS,
    DEFAULT_WAKE_WORDS,
    ECHO_GATE_MAX_WAIT_S,
    ECHO_GATE_WINDOW_S,
    ECHO_RMS_FLOOR,
    ENROLL_NUDGE_COOLDOWN_S,
    FRAME_DURATION_MS,
    MAX_SESSION_DURATION_S,
    PRE_ROLL_FRAMES,
    RMS_THRESHOLD,
    SESSION_COOLDOWN_S,
    SILENCE_TIMEOUT_S,
    SILERO_MODEL_PATH,
    SILERO_VAD_ENABLED,
    SPEECH_HOLDOFF_S,
    STT_KEEPALIVE,
    STT_RATE,
    WEBRTCVAD_AGGRESSIVENESS,
    WEBRTCVAD_ENABLED,
)
from hal.drivers.voice._internal.lamp_sender import LampSender
from hal.drivers.voice._internal.speaker_decorate import SpeakerDecorator
from hal.drivers.voice._internal.vad_filters import SileroVADFilter, WebRTCVADFilter
from hal.drivers.voice.backchannel import Backchannel
from hal.drivers.voice.stt_provider import STTProvider

logger = logging.getLogger("hal.voice")


class VoiceService:
    """Local VAD + pluggable STT provider for autonomous sensing."""

    # Strip HW markers, audio tags, and system tags from realtime agent output.
    RT_MARKER_RE: re.Pattern[str] = re.compile(
        r"\[HW:/[^\]]*\]"
        r"|\[(?:laughs|LAUGHS|sighs|chuckle|light chuckle|giggle|big laugh|gasps|gulps|breathes|clears throat|whispers|pauses|hesitates|stammers)"
        r"[^\]]*\]"
        r"|\[(?:cheerfully|playfully|quietly|nervously|deadpan|flatly|dramatic tone|resigned tone|excited|calm|tired|sad|sorrowful|nervous|frustrated)"
        r"[^\]]*\]"
        r"|`\[[^\]]*\]`"
        r"|NO_REPLY",
        re.IGNORECASE,
    )

    @staticmethod
    def strip_rt_markers(text: str) -> str:
        """Remove HW markers, audio tags, and system tags from realtime agent text."""
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
    ):
        self._stt = stt_provider
        self._input_device = input_device
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._listening = False
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
            WebRTCVADFilter(WEBRTCVAD_AGGRESSIVENESS, self._np)
            if WEBRTCVAD_ENABLED
            else None
        )
        if not WEBRTCVAD_ENABLED:
            logger.info("WebRTC VAD disabled (HAL_WEBRTCVAD_ENABLED=false)")

        self._silero_vad = (
            SileroVADFilter(SILERO_MODEL_PATH, self._np) if SILERO_VAD_ENABLED else None
        )
        if not SILERO_VAD_ENABLED:
            logger.info("Silero VAD disabled via HAL_SILERO_ENABLED=false")

        # Speaker decoration (wake-word + speaker recognizer + SER)
        self._decorator = SpeakerDecorator(
            wake_words=list(wake_words) if wake_words else list(DEFAULT_WAKE_WORDS),
            nudge_cooldown_s=ENROLL_NUDGE_COOLDOWN_S,
        )

        # Lamp Server event sender (with echo similarity filter)
        self._lamp_sender = LampSender(tts_service=tts_service)

        # Realtime voice agent — parallel audio pipeline (Gemini Live / OpenAI Realtime).
        self._realtime = RealtimeOrchestrator()

        # Hook into TTS on_speak_end to feed spoken text back to the realtime agent.
        # With turn_complete=False on text inputs, this won't trigger a standalone response.
        if tts_service is not None:
            original_on_speak_end = tts_service._on_speak_end

            def _tts_speak_end_with_realtime_feedback() -> None:
                if original_on_speak_end:
                    original_on_speak_end()
                if (
                    hal_config.REALTIME_ENABLED
                    and self._realtime.available
                    and tts_service.last_spoken_text
                ):
                    text: str = tts_service.last_spoken_text
                    logger.info("[realtime] Feeding TTS history: %r", text[:100])
                    self._realtime.send_text(f"[TTS HISTORY] {text}")

            tts_service._on_speak_end = _tts_speak_end_with_realtime_feedback

    def set_music_service(self, music_service) -> None:
        self._music = music_service

    def set_wake_words(self, words: list) -> None:
        """Update wake word list at runtime (called when agent is renamed)."""
        self._decorator.set_wake_words(words)

    @property
    def available(self) -> bool:
        return self._sd is not None and self._np is not None and self._stt.available

    @property
    def listening(self) -> bool:
        return self._listening

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
        if hal_config.REALTIME_ENABLED:
            self._realtime.start()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="voice")
        self._thread.start()
        logger.info("VoiceService started (local VAD + %s)", self._stt.name)

    def stop(self):
        self._running = False
        if hal_config.REALTIME_ENABLED:
            self._realtime.stop()
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
                    samplerate=STT_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=512,
                ):
                    pass
                logger.info(
                    "Audio device opened at %dHz natively (no resample needed)",
                    STT_RATE,
                )
                return STT_RATE
            except Exception:
                logger.info(
                    "Audio device native rate: %dHz (will resample to %dHz for STT)",
                    native,
                    STT_RATE,
                )
                return native
        except Exception as e:
            logger.warning(
                "Could not detect device rate, defaulting to %dHz: %s", STT_RATE, e
            )
            return STT_RATE

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
        if BARGE_IN_ENABLED:
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
        logger.info("TTS done, waiting for reverb decay (RMS < %d)...", ECHO_RMS_FLOOR)
        np = self._np
        device_rate = self._device_rate or STT_RATE
        window_frames = int(device_rate * ECHO_GATE_WINDOW_S)
        try:
            # Prefer arecord backend (same as recording loop) — avoids PortAudio rate errors
            if self._alsa_device is not None:
                mic_ctx = ArecordStream(
                    alsa_device=self._alsa_device,
                    rate=device_rate,
                    channels=CHANNELS,
                    blocksize=window_frames,
                    np=np,
                )
            else:
                mic_ctx = self._sd.InputStream(
                    samplerate=device_rate,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=window_frames,
                    device=self._input_device,
                )
            elapsed = 0.0
            with mic_ctx as tmp_mic:
                while elapsed < ECHO_GATE_MAX_WAIT_S and self._running:
                    data, overflowed = tmp_mic.read(window_frames)
                    if overflowed:
                        continue
                    measured = float(np.sqrt(np.mean(data.astype(np.float32) ** 2)))
                    elapsed += ECHO_GATE_WINDOW_S
                    if measured < ECHO_RMS_FLOOR:
                        logger.info(
                            "Reverb decayed (RMS=%.0f < %d) after %.2fs",
                            measured,
                            ECHO_RMS_FLOOR,
                            elapsed,
                        )
                        return
            logger.info(
                "Reverb gate timeout after %.1fs, resuming anyway", ECHO_GATE_MAX_WAIT_S
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
            BARGE_IN_RMS_THRESHOLD,
            BARGE_IN_TRIGGER_FRAMES,
            BARGE_IN_BLOCK_MS,
        )
        np = self._np
        device_rate = self._device_rate or STT_RATE
        frame_size = int(device_rate * BARGE_IN_BLOCK_MS / 1000)
        consecutive = 0
        max_seen = 0.0  # diagnostic: peak RMS observed during this monitor session
        try:
            if self._alsa_device is not None:
                mic_ctx = ArecordStream(
                    alsa_device=self._alsa_device,
                    rate=device_rate,
                    channels=CHANNELS,
                    blocksize=frame_size,
                    np=np,
                )
            else:
                mic_ctx = self._sd.InputStream(
                    samplerate=device_rate,
                    channels=CHANNELS,
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
                    if measured > BARGE_IN_RMS_THRESHOLD:
                        consecutive += 1
                        if consecutive >= BARGE_IN_TRIGGER_FRAMES:
                            logger.info(
                                "BARGE-IN: RMS=%.0f > %d for %d frames → stop TTS",
                                measured,
                                BARGE_IN_RMS_THRESHOLD,
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
        time.sleep(0.5)  # Brief pause for audio subsystem to settle

        # Use arecord only when explicitly configured via HAL_AUDIO_INPUT_ALSA.
        # Auto-detection is disabled because arecord uses exclusive ALSA access,
        # which conflicts with SoundPerception's sd.rec() calls on the same device
        # (both try to open plughw:X,0 — one silently reads zeros and STT never fires).
        # Auto-detection is safe only on Pi5 where SoundPerception is not using the mic.
        # Set HAL_AUDIO_INPUT_ALSA=plughw:X,0 in .env to opt in explicitly.
        if self._alsa_device is not None:
            device_rate = STT_RATE  # plughw does SRC; record directly at STT rate
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

        frame_size = int(device_rate * FRAME_DURATION_MS / 1000)
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
                        channels=CHANNELS,
                        blocksize=frame_size,
                        np=self._np,
                    )
                else:
                    mic_ctx = self._sd.InputStream(
                        samplerate=device_rate,
                        channels=CHANNELS,
                        dtype="int16",
                        blocksize=frame_size,
                        device=self._input_device,
                    )
                with mic_ctx as mic:
                    logger.info(
                        "Listening for speech (RMS=%d, rate=%dHz, backend=%s)...",
                        RMS_THRESHOLD,
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
        Breaks out when TTS starts speaking so _loop can close mic and reopen after."""
        speech_start = None
        speech_pre_buffer = []  # frames buffered during holdoff period
        lookback = deque(maxlen=PRE_ROLL_FRAMES)

        # Keepalive: pre-connect STT WS so it's ready before speech is detected.
        keepalive_session = None
        if STT_KEEPALIVE:
            keepalive_session = self._stt.create_session()
            if not keepalive_session.start(lambda text, is_final: None):
                keepalive_session = None
            else:
                logger.info("STT keepalive: pre-connected, waiting for speech...")

        while self._running:
            # Yield mic to TTS or music — break so _loop closes InputStream first
            if self._tts_is_speaking() or self._music_is_playing():
                logger.info("TTS/music started, releasing mic...")
                if keepalive_session:
                    keepalive_session.close()
                return

            data, overflowed = mic.read(frame_size)
            if overflowed:
                continue

            # Re-check after blocking read — music/TTS may have started during mic.read
            if self._tts_is_speaking() or self._music_is_playing():
                return

            # Append to lookback for pre-roll.
            lookback.append(data)

            energy = rms(data, self._np)

            if energy >= RMS_THRESHOLD and self._webrtcvad_is_speech(data, device_rate):
                if speech_start is None:
                    speech_start = time.time()
                    speech_pre_buffer = [data]
                else:
                    speech_pre_buffer.append(data)
                # Wait for holdoff before connecting STT (avoid short noises)
                if (time.time() - speech_start) >= SPEECH_HOLDOFF_S:
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
                        len(history) * FRAME_DURATION_MS,
                        buffered,
                    )
                    speech_pre_buffer = [
                        resample_to_stt(f, device_rate, STT_RATE, self._np)
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
                    time.sleep(SESSION_COOLDOWN_S)
                    # Pre-connect next session immediately
                    if STT_KEEPALIVE and self._running and not self._tts_is_speaking():
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
                if energy >= RMS_THRESHOLD:
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
        stt_session = preconnected_session or self._stt.create_session()

        longest_partial = [""]
        final_segments = []
        final_sent = [False]
        # One-shot per session: fire emotion=listening on the first STT
        # partial so the lamp leans forward + LED blue-pulses while the user
        # is talking. Not on mic-open — that would fire on silence-only
        # false starts (wake word noise, accidental button press).
        listening_emotion_sent = [False]
        # Collect every resampled 16kHz int16 PCM chunk so we can identify the
        # speaker at session end. This list is LOCAL to _stream_session — a
        # fresh empty list every call, no cross-session carry-over.
        audio_buffer: list[bytes] = []
        pre_frames_from_vad = len(speech_pre_buffer or [])
        logger.info(
            "Session START — pre_from_vad=%d frames, device_rate=%dHz",
            pre_frames_from_vad,
            device_rate,
        )

        def on_transcript(text: str, is_final: bool):
            if not is_final:
                logger.info("STT partial: '%s'", text)
                if len(text) > len(longest_partial[0]):
                    longest_partial[0] = text
                self._backchannel.on_partial(text)
                if not listening_emotion_sent[0]:
                    listening_emotion_sent[0] = True
                    try:
                        requests.post(
                            "http://127.0.0.1:5001/emotion",
                            json={"emotion": "listening"},
                            timeout=0.3,
                        )
                    except Exception as e:
                        logger.warning("listening emotion trigger failed: %s", e)
                return
            # Accumulate final segments — don't send yet, wait for session close.
            # Flux model fires multiple EndOfTurn events for natural pauses within
            # one utterance, so sending immediately would split a single sentence.
            logger.info("STT final segment: '%s'", text)
            # Store final text + any partial accumulated before this final.
            # After final, STT resets partials to empty, so save longest_partial now.
            best = longest_partial[0] if len(longest_partial[0]) > len(text) else text
            if best:
                final_segments.append(best)
            longest_partial[0] = ""
            final_sent[0] = True

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
                        resample_to_stt(data, device_rate, STT_RATE, self._np)
                    )

            if not connect_ok[0]:
                return

            rt_audio_buffer: list = []
            # Flush holdoff audio (frames captured before STT connect, both paths)
            all_pre = (speech_pre_buffer or []) + pre_buffer
            if all_pre:
                logger.info(
                    "Session FILL (pre-flush) — added %d frames (~%.0fms) to buffer",
                    len(all_pre),
                    len(all_pre) * FRAME_DURATION_MS,
                )
                for frame in all_pre:
                    stt_session.send_audio(frame)
                    audio_buffer.append(frame)
                    # Also send pre-buffer to realtime model (non-blocking queue put)
                    if hal_config.REALTIME_ENABLED and self._realtime.available:
                        audio_f32 = pcm16_bytes_to_float32(frame)
                        audio_f32 = resample_float32(audio_f32, STT_RATE, self._realtime.sample_rate)
                        self._realtime.append_audio(audio_f32)
                        rt_audio_buffer.append(audio_f32)

            self._listening = True
            last_speech_time = time.time()
            session_start = time.time()
            # Track index of last frame with speech energy — used to trim
            # trailing silence from the speaker-recognition buffer at session
            # end. SILENCE_TIMEOUT_S holds the session open for ~2.5s after
            # the user stops, so without this the voiceprint ends up 30-50%
            # silence and the embedding degrades.
            last_speech_idx: int = len(audio_buffer) - 1
            # Signal Lamp to show listening LED as soon as mic session opens (before transcript arrives)
            try:
                requests.post(
                    "http://127.0.0.1:5000/api/sensing/event",
                    json={"type": "voice_listening", "message": "listening"},
                    timeout=0.3,
                )
            except Exception:
                pass

            while self._running and not stt_session.is_closed():
                # If TTS or music starts mid-session, stop streaming immediately
                if self._tts_is_speaking():
                    logger.info("TTS started mid-session, closing STT to avoid echo")
                    break
                if self._music_is_playing():
                    logger.info("Music started mid-session, closing STT")
                    break

                # Guard against zombie sessions
                if (time.time() - session_start) > MAX_SESSION_DURATION_S:
                    logger.warning(
                        "STT session exceeded %ds, force-closing",
                        MAX_SESSION_DURATION_S,
                    )
                    break

                data, overflowed = mic.read(frame_size)
                if overflowed:
                    continue

                resampled = resample_to_stt(data, device_rate, STT_RATE, self._np)
                try:
                    stt_session.send_audio(resampled)
                except Exception as e:
                    logger.warning("send_audio failed (connection dead?): %s", e)
                    break
                audio_buffer.append(resampled)

                # Parallel: stream to realtime model (non-blocking queue put)
                if hal_config.REALTIME_ENABLED and self._realtime.available:
                    audio_f32 = pcm16_bytes_to_float32(resampled)
                    audio_f32 = resample_float32(audio_f32, STT_RATE, self._realtime.sample_rate)
                    self._realtime.append_audio(audio_f32)
                    rt_audio_buffer.append(audio_f32)

                energy = rms(data, self._np)
                if energy >= RMS_THRESHOLD:
                    last_speech_time = time.time()
                    last_speech_idx = len(audio_buffer) - 1
                elif (time.time() - last_speech_time) > SILENCE_TIMEOUT_S:
                    logger.info("Silence detected, disconnecting STT")
                    break
        except Exception as e:
            logger.error("STT stream error: %s", e)
        finally:
            self._backchannel.reset()
            self._listening = False
            stt_session.close()
            # Combine all final segments + any trailing partial into one transcript.
            if longest_partial[0]:
                final_segments.append(longest_partial[0])
            combined = " ".join(final_segments).strip()

            # Snapshot the FULL (untrimmed) buffer for SER before trimming.
            ser_audio_buffer = list(audio_buffer)

            # Remove trailing silence from audio_buffer for speaker recognition.
            # Leaves a 200ms tail for word endings; STT buffer unaffected.
            if last_speech_idx >= 0:
                tail_frames = int(200 / FRAME_DURATION_MS) + 1
                trim_end = min(last_speech_idx + tail_frames + 1, len(audio_buffer))
                dropped = len(audio_buffer) - trim_end
                if dropped > 0:
                    del audio_buffer[trim_end:]
                    logger.info(
                        "Session TRIM — dropped %d trailing-silence frames (~%.2fs) "
                        "[speaker-recog buffer only; SER keeps full %d frames]",
                        dropped,
                        dropped * FRAME_DURATION_MS / 1000,
                        len(ser_audio_buffer),
                    )

            # Final snapshot of the buffer for traceability before it goes
            # out of scope. 1 session = 1 speaking turn = this many frames.
            buf_frames = len(audio_buffer)
            buf_bytes = sum(len(b) for b in audio_buffer)
            buf_duration = buf_bytes / (STT_RATE * 2)
            logger.info(
                "Session END — buffer frames=%d bytes=%d duration=%.2fs transcript=%r",
                buf_frames,
                buf_bytes,
                buf_duration,
                combined or "(empty)",
            )

            # --- Realtime voice agent (runs first, before speaker ID / Lamp) ---
            # Runs even if STT transcript is empty — the model has the raw audio.
            rt_delegated = False
            rt_handled = False
            rt_transcript = ""
            if hal_config.REALTIME_ENABLED and self._realtime.available and rt_audio_buffer:
                logger.info("[realtime] Entering realtime flow — committing audio (stt=%r)",
                            combined[:100] if combined else "(empty)")
                try:
                    # Inject per-turn context before committing
                    turn_ctx: list[str] = [
                        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S %A')}",
                    ]
                    try:
                        if hal_app_state.sensing_service:
                            cu: str = hal_app_state.sensing_service._perception_orchestrator.current_user or ""
                            if cu:
                                turn_ctx.append(f"Current user: {cu}")
                    except Exception:
                        pass
                    self._realtime.send_text("[TURN CONTEXT] " + " | ".join(turn_ctx))

                    self._realtime.commit_audio()
                    logger.info("[realtime] Audio committed — streaming output")
                    text_parts: list[str] = []
                    sentence_buf: str = ""
                    first_sentence_sent: bool = False
                    SENTENCE_ENDS = (".", "!", "?", "。", "！", "？")

                    rt_delegate_msg: str = ""
                    for output in self._realtime.stream_output():
                        if isinstance(output, DelegateSignal):
                            rt_delegated = True
                            rt_delegate_msg = output.message
                            break
                        if isinstance(output, RTTextOutput):
                            text_parts.append(output.text)
                            sentence_buf += output.text
                            # Flush complete sentences to TTS as they arrive
                            if self._tts is not None and sentence_buf.rstrip().endswith(SENTENCE_ENDS):
                                sentence: str = self.strip_rt_markers(sentence_buf)
                                if sentence:
                                    if not first_sentence_sent:
                                        logger.info("[realtime] First sentence → speak: %r", sentence[:80])
                                        self._tts.speak(sentence)
                                        first_sentence_sent = True
                                    else:
                                        logger.info("[realtime] Next sentence → speak_queue: %r", sentence[:80])
                                        self._tts.speak_queue(sentence)
                                sentence_buf = ""

                    rt_transcript = self.strip_rt_markers("".join(text_parts))

                    if rt_delegated:
                        logger.info("[realtime] Model delegated → will forward to Lamp")
                    else:
                        rt_handled = True
                        # Flush any remaining text that didn't end with a sentence boundary
                        remaining: str = self.strip_rt_markers(sentence_buf)
                        if remaining and self._tts is not None:
                            if not first_sentence_sent:
                                logger.info("[realtime] Final fragment → speak: %r", remaining[:80])
                                self._tts.speak(remaining)
                            else:
                                logger.info("[realtime] Final fragment → speak_queue: %r", remaining[:80])
                                self._tts.speak_queue(remaining)
                        logger.info(
                            "[realtime] Chit-chat complete — transcript=%r",
                            rt_transcript[:200] if rt_transcript else "(empty)",
                        )
                        # Save turn to realtime memory
                        if combined or rt_transcript:
                            self._realtime.save_turn(
                                user_text=combined or "(audio only)",
                                agent_text=rt_transcript or "(audio only)",
                            )
                except Exception as e:
                    logger.warning("[realtime] Processing failed: %s — will forward to Lamp", e)
                    rt_delegated = True  # fall through to Lamp on error
            elif hal_config.REALTIME_ENABLED:
                logger.warning("[realtime] Enabled but agent not available — falling back to Lamp")

            # --- Speaker recognition + Lamp send ---
            from hal.drivers.voice.speech_emotion.constants import UNKNOWN_USER_LABEL

            final_text, event_type = self._decorator.resolve_wake_word_split(combined)
            user = UNKNOWN_USER_LABEL

            if combined:
                final_msg, se_user = self._decorator.identify_and_decorate(
                    final_text, audio_buffer
                )
                user = se_user if se_user else UNKNOWN_USER_LABEL
                logger.info("Final message → Lamp (%s): %r", event_type, final_msg)

                if rt_handled:
                    # Realtime already spoke — send as "voice_handled" to skip dead-air filler.
                    # Include skill hint so OpenClaw reads input-branching and responds NO_REPLY.
                    self._lamp_sender.send(
                        f"[skills: input-branching]\n[HANDLED] {final_msg}\n[REPLY] {rt_transcript}",
                        event_type="voice_agent_handled",
                        skip_echo=True,
                    )
                elif rt_delegated:
                    # Delegated — send voice agent's summary + STT transcript to Lamp
                    if rt_delegate_msg:
                        lamp_msg: str = f"[voice-instruction] {rt_delegate_msg}\n[transcript] {final_msg}"
                    else:
                        lamp_msg = final_msg
                    logger.info("[realtime] Delegated with message: %r", lamp_msg[:100] if lamp_msg else "")
                    if lamp_msg:
                        self._lamp_sender.send(lamp_msg, event_type=event_type)
                else:
                    # Realtime not active — send to Lamp normally
                    self._lamp_sender.send(final_msg, event_type=event_type)

            # 2. Submit SER — uses the UNTRIMMED snapshot so laughter / sighs
            self._decorator.submit_speech_emotion_from_session(
                ser_audio_buffer, user=user
            )

            # Clear listening LED
            try:
                requests.post(
                    "http://127.0.0.1:5000/api/sensing/event",
                    json={"type": "voice_listening_end", "message": "done"},
                    timeout=0.3,
                )
            except Exception:
                pass

            # Safety net: if we fired emotion=listening but no follow-up
            # emotion arrives (LLM error, silence-only after first partial,
            # TTS interrupt before response), blue-pulse would hang. After
            # 8s, reset to idle — but only if current emotion is still
            # "listening" so we don't stomp on a real LLM-driven emotion.
            if listening_emotion_sent[0]:

                def _reset_if_still_listening():
                    try:
                        from hal import app_state

                        if app_state._current_emotion == "listening":
                            requests.post(
                                "http://127.0.0.1:5001/emotion",
                                json={"emotion": "idle"},
                                timeout=0.3,
                            )
                    except Exception as e:
                        logger.warning("listening idle-reset failed: %s", e)

                threading.Timer(8.0, _reset_if_still_listening).start()

            # Buffer is a local variable — once this function returns it is
            # garbage-collected. The next _stream_session call starts with a
            # fresh empty buffer. Leaving this log here as a breadcrumb so
            # operators can confirm session boundaries in the log stream.
            logger.info("Session RESET — audio_buffer discarded, ready for next turn")
