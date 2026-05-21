"""
Voice Service — local VAD + pluggable STT for autonomous sensing.

Pipeline:
  1. Mic always on, local RMS energy check (free, zero cost)
  2. Speech detected → create STT session, stream audio
  3. Silence for SILENCE_TIMEOUT → close session (stop billing)
  4. Transcripts → POST to Lumi Server /api/sensing/event
  5. Lumi Go → local intent match or OpenClaw → AI responds → POST /voice/speak

STT provider is pluggable (default: Deepgram).
"""

import logging
import os
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import requests

from lelamp.service.voice.backchannel import Backchannel
from lelamp.service.voice.stt_provider import STTProvider
from lelamp import config as _lelamp_config

logger = logging.getLogger("lelamp.voice")

LUMI_SENSING_URL = "http://127.0.0.1:5000/api/sensing/event"

STT_RATE = 16000   # Rate expected by all STT providers
CHANNELS = 1
FRAME_DURATION_MS = 64  # Frame duration in ms (device-rate-independent)

# Local VAD config — can be overridden via .env on the device
RMS_THRESHOLD = int(os.environ.get("LELAMP_VAD_THRESHOLD", "3500"))      # RMS above this = speech
SILENCE_TIMEOUT_S = float(os.environ.get("LELAMP_SILENCE_TIMEOUT", "2.5"))  # Silence before STT disconnect
SPEECH_HOLDOFF_S = float(os.environ.get("LELAMP_SPEECH_HOLDOFF", "0.2"))  # Minimum speech duration before connecting STT
# Pre-roll lookback — frames retained BEFORE VAD trigger so quiet first
# syllables ("b", "k", "t", "p" stop consonants, or any utterance starting
# under RMS_THRESHOLD) reach STT instead of getting clipped. 8 × 64ms = 512ms
# of audio history. Equivalent to user manually saying "Uhm..." before speech.
PRE_ROLL_FRAMES = int(os.environ.get("LELAMP_PRE_ROLL_FRAMES", "8"))

SESSION_COOLDOWN_S = float(os.environ.get("LELAMP_SESSION_COOLDOWN_S", "0.3"))

# Silero VAD config
SILERO_VAD_ENABLED = os.environ.get("LELAMP_SILERO_ENABLED", "false").lower() == "true"
SILERO_VAD_THRESHOLD = float(os.environ.get("LELAMP_SILERO_THRESHOLD", "0.3"))
SILERO_CHUNK_SIZE = int(os.environ.get("LELAMP_SILERO_CHUNK_SIZE", "512"))
_SILERO_MODEL_PATH = Path(__file__).parent / "resources" / "silero_vad.onnx"

# WebRTC VAD config — fast C-based pre-filter before Silero (runs in ~0.1ms vs ~20ms)
WEBRTCVAD_ENABLED = os.environ.get("LELAMP_WEBRTCVAD_ENABLED", "false").lower() == "true"
WEBRTCVAD_AGGRESSIVENESS = int(os.environ.get("LELAMP_WEBRTCVAD_AGGRESSIVENESS", "2"))
WEBRTCVAD_FRAME_MS = int(os.environ.get("LELAMP_WEBRTCVAD_FRAME_MS", "30"))

# Echo cancellation config
ECHO_RMS_FLOOR = int(os.environ.get("LELAMP_ECHO_RMS_FLOOR", "200"))
ECHO_GATE_MAX_WAIT_S = float(os.environ.get("LELAMP_ECHO_GATE_MAX_WAIT_S", "1.5"))
ECHO_GATE_WINDOW_S = float(os.environ.get("LELAMP_ECHO_GATE_WINDOW_S", "0.05"))
ECHO_SIMILARITY_THRESHOLD = float(os.environ.get("LELAMP_ECHO_SIMILARITY_THRESHOLD", "0.55"))
ECHO_RELEVANCE_WINDOW_S = float(os.environ.get("LELAMP_ECHO_RELEVANCE_WINDOW_S", "15.0"))
MAX_SESSION_DURATION_S = float(os.environ.get("LELAMP_MAX_SESSION_DURATION_S", "30"))

# Keep-alive mode: pre-connect STT WS before speech is detected so there's no connect delay.
STT_KEEPALIVE = os.environ.get("LELAMP_STT_KEEPALIVE", "false").lower() == "true"

# Speaker recognition — prefix every transcript with "<Name>: " identified from
SPEAKER_RECOGNITION_ENABLED = _lelamp_config.SPEAKER_RECOGNITION_ENABLED
SPEAKER_MIN_AUDIO_S = _lelamp_config.SPEAKER_MIN_AUDIO_S

SPEECH_EMOTION_ENABLED = _lelamp_config.SPEECH_EMOTION_ENABLED

# Wake word patterns (lowercase match) — default for agent named "Lumi"
DEFAULT_WAKE_WORDS = ["hello lumi", "hey lumi", "hey lu mi", "này lumi", "ê lumi", "lumi ơi"]



class _ArecordStream:
    """Drop-in replacement for sd.InputStream using arecord subprocess.

    Records directly via ALSA plughw which handles sample-rate conversion
    natively — the same path as `arecord -D plughw:X,0`.  sounddevice uses
    PortAudio's hw: interface which bypasses ALSA SRC, producing corrupted
    audio at rates the hardware doesn't natively support.
    """

    def __init__(self, alsa_device: str, rate: int, channels: int, blocksize: int, np):
        self._device = alsa_device
        self._rate = rate
        self._channels = channels
        self._blocksize = blocksize
        self._np = np
        self._proc = None
        self._bytes_per_frame = 2 * channels  # int16 = 2 bytes

    def __enter__(self):
        self._proc = subprocess.Popen(
            ["arecord", "-D", self._device, "-f", "S16_LE",
             "-r", str(self._rate), "-c", str(self._channels),
             "-t", "raw", "-q"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        return self

    def __exit__(self, *args):
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except Exception:
                self._proc.kill()
            self._proc = None

    def read(self, frames):
        n_bytes = frames * self._bytes_per_frame
        raw = self._proc.stdout.read(n_bytes)
        if not raw:
            # arecord process died — raise so _loop can restart it
            raise IOError("arecord process exited (stdout EOF)")
        if len(raw) < n_bytes:
            raw = raw + b"\x00" * (n_bytes - len(raw))
        data = self._np.frombuffer(raw, dtype=self._np.int16).reshape(frames, self._channels)
        return data, False


class VoiceService:
    """Local VAD + pluggable STT provider for autonomous sensing."""

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
        self._wake_words: list = list(wake_words) if wake_words else list(DEFAULT_WAKE_WORDS)
        self._wake_words_lock = threading.Lock()
        self._device_rate: Optional[int] = None  # detected once at first use

        self._sd = None
        self._np = None
        # Explicit override from .env → skip auto-detection entirely
        self._alsa_device: Optional[str] = alsa_device or None

        self._backchannel = Backchannel(tts_service)

        # Enroll-nudge cooldown per voiceprint_hash. When the recognizer
        # assigns a stable cluster label to an unknown voice, we stop
        # asking the agent to ask that voice's name again for
        # _NUDGE_COOLDOWN_S so the agent doesn't repeat "who are you?"
        # to the same person every short utterance.
        # In-memory only — resets on restart (acceptable; worst case is
        # one extra prompt after reboot).
        self._last_nudge_time: dict[str, float] = {}
        self._nudge_cooldown_s: float = float(
            os.environ.get("LELAMP_ENROLL_NUDGE_COOLDOWN_S", str(30 * 60))
        )

        # Speaker recognizer (optional). Lazy-initialized — if embedding API
        # isn't configured the prefix is simply skipped.
        self._speaker = None
        if not SPEAKER_RECOGNITION_ENABLED:
            logger.info("Speaker recognizer disabled by LELAMP_SPEAKER_RECOGNITION_ENABLED=false. This is the default value.")
        else:
            try:
                from lelamp.service.voice.speaker_recognizer import SpeakerRecognizer
                self._speaker = SpeakerRecognizer()
                if not self._speaker.available:
                    logger.info(
                        "Speaker recognizer idle — SPEAKER_EMBEDDING_API_URL not set "
                        "(service instance exists but embedding calls will return 'unknown' with an error)"
                    )
                else:
                    logger.info("Speaker recognizer enabled — will prefix every STT final with speaker name")
            except Exception as e:
                logger.warning("Speaker recognizer init failed: %s", e)
                self._speaker = None

        self._speech_emotion = None
        if not SPEECH_EMOTION_ENABLED:
            logger.info("Speech emotion recognition disabled by LELAMP_SPEECH_EMOTION_ENABLED=false")
        else:
            try:
                from lelamp.service.voice.speech_emotion import SpeechEmotionService
                self._speech_emotion = SpeechEmotionService()
                if not self._speech_emotion.available:
                    logger.info(
                        "Speech emotion service idle — DL backend URL not set"
                    )
                else:
                    logger.info("Speech emotion service enabled")
            except Exception as e:
                logger.warning("Speech emotion service init failed: %s", e)
                self._speech_emotion = None

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

        # WebRTC VAD — fast C-based pre-filter (runs before Silero to save CPU)
        # Enable via LELAMP_WEBRTCVAD_ENABLED=true in .env.
        self._webrtcvad: Optional[object] = None
        if WEBRTCVAD_ENABLED:
            try:
                import webrtcvad as _webrtcvad
                self._webrtcvad = _webrtcvad.Vad(WEBRTCVAD_AGGRESSIVENESS)
                logger.info("WebRTC VAD loaded (aggressiveness=%d)", WEBRTCVAD_AGGRESSIVENESS)
            except ImportError:
                logger.warning("webrtcvad not installed — pip install webrtcvad")
            except Exception as e:
                logger.warning("WebRTC VAD not available: %s", e)
        else:
            logger.info("WebRTC VAD disabled (LELAMP_WEBRTCVAD_ENABLED=false)")

        # Silero VAD (ONNX) — secondary speech filter to reject non-speech audio (TV, music, noise)
        # Auto-enabled if model file exists. Disable via LELAMP_SILERO_ENABLED=false in .env.
        self._silero: Optional[object] = None
        self._silero_state: Optional[object] = None
        self._silero_lock = threading.Lock()
        if SILERO_VAD_ENABLED and _SILERO_MODEL_PATH.exists():
            try:
                import os as _os
                _os.environ.setdefault("OMP_NUM_THREADS", "1")
                _os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
                import onnxruntime as ort
                _sess_opts = ort.SessionOptions()
                _sess_opts.intra_op_num_threads = 1
                _sess_opts.inter_op_num_threads = 1
                _sess_opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
                self._silero = ort.InferenceSession(
                    str(_SILERO_MODEL_PATH),
                    sess_options=_sess_opts,
                    providers=["CPUExecutionProvider"],
                )
                self._silero_reset_state()
                logger.info("Silero VAD loaded (threshold=%.2f)", SILERO_VAD_THRESHOLD)
            except Exception as e:
                logger.warning("Silero VAD not available — falling back to RMS only: %s", e)
        elif not _SILERO_MODEL_PATH.exists():
            logger.info("Silero VAD model not found — using RMS only")
        else:
            logger.info("Silero VAD disabled via LELAMP_SILERO_ENABLED=false")

    def set_music_service(self, music_service) -> None:
        self._music = music_service

    def set_wake_words(self, words: list) -> None:
        """Update wake word list at runtime (called when agent is renamed)."""
        with self._wake_words_lock:
            self._wake_words = [w.lower() for w in words]
        logger.info("Wake words updated: %s", self._wake_words)

    @property
    def available(self) -> bool:
        return (
                self._sd is not None
                and self._np is not None
                and self._stt.available
        )

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
        self._thread = threading.Thread(target=self._loop, daemon=True, name="voice")
        self._thread.start()
        logger.info("VoiceService started (local VAD + %s)", self._stt.name)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("VoiceService stopped")

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
            # check_input_settings can fail even when ALSA can handle it, so just try opening.
            try:
                with sd.InputStream(device=self._input_device, samplerate=STT_RATE,
                                    channels=CHANNELS, dtype="int16", blocksize=512):
                    pass
                logger.info("Audio device opened at %dHz natively (no resample needed)", STT_RATE)
                return STT_RATE
            except Exception:
                logger.info("Audio device native rate: %dHz (will resample to %dHz for STT)", native, STT_RATE)
                return native
        except Exception as e:
            logger.warning("Could not detect device rate, defaulting to %dHz: %s", STT_RATE, e)
            return STT_RATE

    def _resample_to_stt(self, data, device_rate: int):
        """Resample audio from device_rate to STT_RATE with proper anti-aliasing.
        Uses scipy.signal.resample_poly (polyphase + anti-aliasing FIR filter).
        Returns raw bytes at STT_RATE. No-op if rates already match."""
        if device_rate == STT_RATE:
            return data.tobytes()
        from math import gcd
        import scipy.signal
        samples = data.flatten().astype(self._np.float32)
        g = gcd(STT_RATE, device_rate)
        up, down = STT_RATE // g, device_rate // g
        resampled = scipy.signal.resample_poly(samples, up, down).astype(self._np.int16)
        return resampled.tobytes()

    def _rms(self, audio_data) -> float:
        """Calculate RMS energy of audio frame."""
        np = self._np
        samples = audio_data.flatten().astype(np.float32)
        return float(np.sqrt(np.mean(samples ** 2)))

    def _webrtcvad_is_speech(self, data, device_rate: int) -> bool:
        """Run WebRTC VAD on audio frame. Returns True if any 30ms chunk contains speech.
        Falls back to True (pass-through) if webrtcvad is unavailable."""
        if self._webrtcvad is None:
            return True
        try:
            np = self._np
            if device_rate != STT_RATE:
                from math import gcd
                import scipy.signal
                samples = data.flatten().astype(np.float32)
                g = gcd(STT_RATE, device_rate)
                audio_16k = scipy.signal.resample_poly(samples, STT_RATE // g, device_rate // g).astype(np.int16)
            else:
                audio_16k = data.flatten().astype(np.int16)
            frame_samples = int(STT_RATE * WEBRTCVAD_FRAME_MS / 1000)  # 480 @ 16kHz/30ms
            raw = audio_16k.tobytes()
            frame_bytes = frame_samples * 2  # int16 = 2 bytes
            for i in range(0, len(raw) - frame_bytes + 1, frame_bytes):
                if self._webrtcvad.is_speech(raw[i:i + frame_bytes], STT_RATE):
                    return True
            return False
        except Exception as e:
            logger.warning("WebRTC VAD error: %s", e)
            return True

    def _silero_reset_state(self):
        """Reset Silero LSTM state and context between speech segments."""
        import numpy as np
        self._silero_state = np.zeros((2, 1, 128), dtype=np.float32)
        # Silero v5+ requires 64 context samples (16kHz) prepended to each chunk
        self._silero_context = np.zeros((1, 64), dtype=np.float32)

    def _silero_is_speech(self, data: "np.ndarray", device_rate: int) -> bool:
        """Run Silero VAD on audio frame. Returns True if speech detected.
        Falls back to True (pass-through) if model is unavailable."""
        if self._silero is None:
            return True
        try:
            import numpy as np
            # Resample to 16kHz for silero (same target as STT)
            if device_rate != STT_RATE:
                from math import gcd
                import scipy.signal
                samples = data.flatten().astype(np.float32)
                g = gcd(STT_RATE, device_rate)
                up, down = STT_RATE // g, device_rate // g
                audio_16k = scipy.signal.resample_poly(samples, up, down).astype(np.float32)
            else:
                audio_16k = data.flatten().astype(np.float32)

            # Normalize int16 → float32 [-1, 1]
            audio_norm = audio_16k / 32768.0

            # Run inference in 512-sample chunks, keep max confidence
            max_conf = 0.0
            with self._silero_lock:
                for i in range(0, len(audio_norm), SILERO_CHUNK_SIZE):
                    chunk = audio_norm[i:i + SILERO_CHUNK_SIZE]
                    if len(chunk) < SILERO_CHUNK_SIZE:
                        chunk = np.pad(chunk, (0, SILERO_CHUNK_SIZE - len(chunk)))
                    # Silero v5+: prepend 64-sample context from previous chunk
                    x = np.concatenate([self._silero_context, chunk.reshape(1, -1)], axis=1)
                    out = self._silero.run(
                        None,
                        {
                            "input": x,
                            "state": self._silero_state,
                            "sr": np.array(STT_RATE, dtype=np.int64),
                        },
                    )
                    max_conf = max(max_conf, float(out[0][0][0]))
                    self._silero_state = out[1]
                    self._silero_context = x[:, -64:]

            is_speech = max_conf >= SILERO_VAD_THRESHOLD
            if not is_speech:
                logger.info("Silero: conf=%.3f < threshold=%.2f — rejected", max_conf, SILERO_VAD_THRESHOLD)
            return is_speech
        except Exception as e:
            logger.warning("Silero VAD inference error: %s", e)
            return True  # fail open — don't block speech

    def _tts_is_speaking(self) -> bool:
        """Check if TTS is currently using the audio device."""
        return self._tts is not None and self._tts.speaking

    def _music_is_playing(self) -> bool:
        """Check if music is currently playing."""
        return self._music is not None and self._music.playing

    @staticmethod
    def _should_request_speaker_enroll(
        transcript: str,
        duration_s: float = 0.0,
        min_words: int = 10,
        min_duration_s: float = 2.0,
    ) -> bool:
        """Whether unknown-speaker message should include a strong enroll nudge."""
        return len(transcript.split()) >= min_words and duration_s >= min_duration_s

    def _format_unknown_speaker_message(
        self,
        transcript: str,
        audio_path: str,
        duration_s: float = 0.0,
        voiceprint_hash: Optional[str] = None,
    ) -> str:
        """Format Lumi message for an unrecognized speaker (enroll hints, cooldown)."""
        now = time.time()
        in_cooldown = False
        if voiceprint_hash:
            last = self._last_nudge_time.get(voiceprint_hash, 0.0)
            if now - last < self._nudge_cooldown_s:
                in_cooldown = True
                logger.info(
                    "Enroll nudge skipped for %s — asked %.0fs ago "
                    "(cooldown %.0fs); path + tag still surfaced",
                    voiceprint_hash, now - last, self._nudge_cooldown_s,
                )

        hash_tag = f" [voice:{voiceprint_hash}]" if voiceprint_hash else ""
        audio_hint = f" (audio saved at {audio_path})" if audio_path else ""

        if in_cooldown:
            return f"Unknown Speaker:{hash_tag} {transcript}{audio_hint}"

        if audio_path and self._should_request_speaker_enroll(transcript, duration_s):
            if voiceprint_hash:
                self._last_nudge_time[voiceprint_hash] = now
            return (
                f"Unknown Speaker:{hash_tag} {transcript} "
                f"(audio save at {audio_path}, auto enroll this speaker "
                f"if having speaker name in transcript, else ask user's name)"
            )

        return (
            f"Unknown Speaker:{hash_tag} {transcript} "
            f"(audio saved at {audio_path}. Note: audio is too short for "
            f"single enrollment. If prior turns tagged the same {voiceprint_hash or 'voice cluster'}, "
            f"combine their saved paths with this one when enrolling; "
            f"otherwise ask the user to introduce themselves longer.)"
        )

    def _session_wav_for_ser(
        self, audio_buffer: list[bytes],
    ) -> Optional[tuple[bytes, float]]:
        """Build mono 16 kHz WAV + duration from the STT session buffer (for SER)."""
        if not audio_buffer:
            return None
        duration_s = sum(len(b) for b in audio_buffer) / (STT_RATE * 2)
        if duration_s < SPEAKER_MIN_AUDIO_S:
            return None
        try:
            from lelamp.service.voice.speaker_recognizer.speaker_recognizer import (
                pcm16_bytes_to_wav,
            )
        except Exception as e:
            logger.warning("Session WAV for SER skipped — helper import failed: %s", e)
            return None
        try:
            return pcm16_bytes_to_wav(b"".join(audio_buffer), STT_RATE), duration_s
        except Exception as e:
            logger.warning("Session WAV for SER failed: %s", e)
            return None

    def _submit_speech_emotion_from_session(
        self,
        audio_buffer: list[bytes],
        user: str,
    ) -> None:
        """Submit SER on the full mic-session buffer."""
        if self._speech_emotion is None or not self._speech_emotion.available:
            logger.info(
                "Speech emotion submit skipped: service_init=%s available=%s",
                self._speech_emotion is not None,
                bool(self._speech_emotion and self._speech_emotion.available),
            )
            return
        session_audio = self._session_wav_for_ser(audio_buffer)
        if session_audio is None:
            return
        wav_bytes, duration_s = session_audio

        logger.info(
            "Speech emotion submit (session-end): user=%r duration=%.2fs wav=%d bytes",
            user, duration_s, len(wav_bytes),
        )
        try:
            self._speech_emotion.submit(
                user=user, wav_bytes=wav_bytes, duration_s=duration_s,
            )
        except Exception as e:
            logger.warning("Speech emotion submit failed: %s", e)

    def _resolve_wake_word_split(self, combined: str) -> tuple[str, str]:
        """Detect wake word in `combined` and split it off.

        Returns ``(final_text, event_type)``:
        * ``final_text`` — the text that will be sent to Lumi (wake word stripped
          when a command, otherwise the original transcript).
        * ``event_type`` — ``"voice_command"`` when a wake word matched at the
          start, otherwise ``"voice"``. Used as the sensing-event ``type`` field.

        Empty ``combined`` short-circuits to ``("", "voice")``; the caller still
        gets a usable shape but typically skips the Lumi POST.
        """
        if not combined:
            return "", "voice"

        normalized = re.sub(r"[^\w\s]", "", combined.lower())
        with self._wake_words_lock:
            wake_words = list(self._wake_words)
        if not any(w in normalized for w in wake_words):
            return combined, "voice"

        cmd = normalized
        for w in wake_words:
            if cmd.startswith(w):
                cmd = cmd[len(w):].strip()
                break
        return (cmd or combined), "voice_command"

    def _identify_and_decorate(
        self, transcript: str, audio_buffer: list[bytes],
    ) -> tuple[str, Optional[str]]:
        """Run speaker recognition; return (Lumi message, SER user name or None).

        ``user_name`` is set only when speaker recognize completes without
        ``error`` — known label or ``unknown`` for no match. ``None`` skips SER.
        """
        logger.info("Identify and decorate transcript: raw transcript is: '%s'", transcript)
        if self._speaker is None:
            logger.info(
                "Skip speaker ID: recognizer not initialized "
                "(LELAMP_SPEAKER_RECOGNITION_ENABLED or init failure)"
            )
            return transcript, None
        if not audio_buffer:
            logger.warning("Skip speaker ID: audio buffer is empty (no frames captured this session)")
            return transcript, None
        try:
            from lelamp.service.voice.speech_emotion.constants import UNKNOWN_USER_LABEL
            from lelamp.service.voice.speaker_recognizer.speaker_recognizer import pcm16_bytes_to_wav
        except Exception as e:
            logger.warning("Skip speaker ID: helper import failed: %s", e)
            return transcript, None

        total_bytes = sum(len(b) for b in audio_buffer)
        duration_s = total_bytes / (STT_RATE * 2)  # int16 mono
        if duration_s < SPEAKER_MIN_AUDIO_S:
            logger.info(
                "Skip speaker ID: only %.2fs of audio buffered (<%.2fs)",
                duration_s, SPEAKER_MIN_AUDIO_S,
            )
            return transcript, None

        try:
            wav_bytes = pcm16_bytes_to_wav(b"".join(audio_buffer), STT_RATE)
            import base64 as _b64
            audio_b64 = _b64.b64encode(wav_bytes).decode("ascii")
            result = self._speaker.recognize(audio_b64, source_type="base64")
        except Exception as e:
            logger.warning("Speaker recognize failed: %s", e)
            return transcript, None

        logger.info("Speaker recognize result: %r", result)
        err = result.get("error")
        audio_path = result.get("unknown_audio_path", "")
        vp_hash = result.get("voiceprint_hash")
        if err:
            logger.warning("Speaker ID skipped — embedding server issue: %s", err)
            if audio_path:
                return self._format_unknown_speaker_message(
                    transcript, audio_path, duration_s, vp_hash,
                ), None
            return transcript, None

        name = result.get("name", "unknown")
        confidence = result.get("confidence", 0.0)
        if result.get("match") and name and name != "unknown":
            display = result.get("display_name") or name.capitalize()
            logger.info(
                "Speaker ID: %s (confidence=%.2f, audio=%s)",
                name, confidence, audio_path or "-",
            )
            return f"Speaker - {display}: {transcript}", name

        logger.info(
            "Speaker ID: unknown (best=%.2f, audio=%s, hash=%s)",
            confidence, audio_path or "-", vp_hash or "-",
        )
        return self._format_unknown_speaker_message(
            transcript, audio_path, duration_s, vp_hash,
        ), UNKNOWN_USER_LABEL

    def _wait_for_tts(self):
        """Block until TTS finishes speaking, then wait for reverb to decay (adaptive RMS gate)."""
        if not self._tts_is_speaking():
            return
        logger.info("TTS is speaking, pausing mic until done...")
        while self._running and self._tts_is_speaking():
            time.sleep(0.2)
        if not self._running:
            return

        # Adaptive RMS gate: wait for reverb/echo to decay instead of fixed sleep
        logger.info("TTS done, waiting for reverb decay (RMS < %d)...", ECHO_RMS_FLOOR)
        np = self._np
        device_rate = self._device_rate or STT_RATE
        window_frames = int(device_rate * ECHO_GATE_WINDOW_S)
        try:
            # Prefer arecord backend (same as recording loop) — avoids PortAudio rate errors
            if self._alsa_device is not None:
                mic_ctx = _ArecordStream(
                    alsa_device=self._alsa_device, rate=device_rate,
                    channels=CHANNELS, blocksize=window_frames, np=np,
                )
            else:
                mic_ctx = self._sd.InputStream(
                    samplerate=device_rate, channels=CHANNELS, dtype="int16",
                    blocksize=window_frames, device=self._input_device,
                )
            elapsed = 0.0
            with mic_ctx as tmp_mic:
                while elapsed < ECHO_GATE_MAX_WAIT_S and self._running:
                    data, overflowed = tmp_mic.read(window_frames)
                    if overflowed:
                        continue
                    rms = float(np.sqrt(np.mean(data.astype(np.float32) ** 2)))
                    elapsed += ECHO_GATE_WINDOW_S
                    if rms < ECHO_RMS_FLOOR:
                        logger.info("Reverb decayed (RMS=%.0f < %d) after %.2fs", rms, ECHO_RMS_FLOOR, elapsed)
                        return
            logger.info("Reverb gate timeout after %.1fs, resuming anyway", ECHO_GATE_MAX_WAIT_S)
        except Exception as e:
            logger.warning("RMS gate failed, falling back to fixed delay: %s", e)
            time.sleep(1.0)

    def _loop(self):
        """Main loop: local VAD → STT on speech → disconnect on silence."""
        time.sleep(0.5)  # Brief pause for audio subsystem to settle

        # Use arecord only when explicitly configured via LELAMP_AUDIO_INPUT_ALSA.
        # Auto-detection is disabled because arecord uses exclusive ALSA access,
        # which conflicts with SoundPerception's sd.rec() calls on the same device
        # (both try to open plughw:X,0 — one silently reads zeros and STT never fires).
        # Auto-detection is safe only on Pi5 where SoundPerception is not using the mic.
        # Set LELAMP_AUDIO_INPUT_ALSA=plughw:X,0 in .env to opt in explicitly.
        if self._alsa_device is not None:
            device_rate = STT_RATE  # plughw does SRC; record directly at STT rate
            logger.info("Using arecord backend (%s) at %dHz", self._alsa_device, device_rate)
        else:
            if self._device_rate is None:
                self._device_rate = self._detect_device_rate()
            device_rate = self._device_rate
            logger.info("Using sounddevice backend (device=%s) at %dHz", self._input_device, device_rate)

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
                    mic_ctx = _ArecordStream(
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
                        RMS_THRESHOLD, device_rate,
                        f"arecord({self._alsa_device})" if self._alsa_device else f"sd({self._input_device})",
                    )
                    self._vad_loop(mic, frame_size, device_rate)
            except Exception as e:
                logger.warning("Voice loop error: %s", e)
                if self._running:
                    time.sleep(3)

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

            rms = self._rms(data)

            if rms >= RMS_THRESHOLD and self._webrtcvad_is_speech(data, device_rate):
                if speech_start is None:
                    speech_start = time.time()
                    speech_pre_buffer = [data]
                else:
                    speech_pre_buffer.append(data)
                # Wait for holdoff before connecting STT (avoid short noises)
                if (time.time() - speech_start) >= SPEECH_HOLDOFF_S:
                    # Run Silero on accumulated buffer (needs multiple chunks for LSTM)
                    if self._silero is not None:
                        combined = self._np.concatenate(speech_pre_buffer)
                        if not self._silero_is_speech(combined, device_rate):
                            speech_start = None
                            speech_pre_buffer = []
                            continue
                    # Prepend pre-trigger history from lookback.
                    buffered = len(speech_pre_buffer)
                    history = list(lookback)[:-buffered] if buffered > 0 else list(lookback)
                    all_frames = history + speech_pre_buffer
                    logger.info(
                        "Speech detected (RMS=%.0f) — pre-roll=%d frames (~%dms) + holdoff=%d frames",
                        rms, len(history), len(history) * FRAME_DURATION_MS, buffered,
                    )
                    speech_pre_buffer = [self._resample_to_stt(f, device_rate) for f in all_frames]
                    self._stream_session(mic, frame_size, device_rate,
                                        preconnected_session=keepalive_session,
                                        speech_pre_buffer=speech_pre_buffer)
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
                            logger.info("STT keepalive: pre-connected, waiting for speech...")
            else:
                speech_start = None
                speech_pre_buffer = []
                if rms >= RMS_THRESHOLD:
                    logger.debug("VAD: RMS=%.0f above threshold but Silero rejected — not speech", rms)

    def _stream_session(self, mic, frame_size: int, device_rate: int, preconnected_session=None, speech_pre_buffer=None):
        """Stream audio to STT provider until silence or TTS interrupts.

        Buffer lifecycle (one per call):
            START  — ``audio_buffer = []`` created as a local variable
            FILL   — every frame that goes to STT is also appended here
            USE    — at session end the finally block reads it for speaker ID + SER
            END    — function returns → local ``audio_buffer`` goes out of
                     scope → garbage-collected. NO state leaks to the next
                     ``_stream_session`` call.
        """
        session = preconnected_session or self._stt.create_session()

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
            pre_frames_from_vad, device_rate,
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
                session._on_transcript_cb = on_transcript
                logger.info("STT keepalive: reusing pre-connected session")
            else:
                # Connect WS in background while buffering mic audio so speech start isn't lost.
                pass

            connect_ok = [False]
            connect_done = threading.Event()

            def _do_connect():
                connect_ok[0] = session.start(on_transcript)
                connect_done.set()

            if preconnected_session:
                connect_ok[0] = True
                connect_done.set()
            else:
                threading.Thread(target=_do_connect, daemon=True, name="stt-connect").start()

            pre_buffer = []
            while not connect_done.wait(timeout=0.005):
                if self._tts_is_speaking():
                    connect_done.wait(timeout=2)
                    break
                data, overflowed = mic.read(frame_size)
                if not overflowed:
                    pre_buffer.append(self._resample_to_stt(data, device_rate))

            if not connect_ok[0]:
                return

            # Flush holdoff audio (frames captured before STT connect, both paths)
            all_pre = (speech_pre_buffer or []) + pre_buffer
            if all_pre:
                logger.info(
                    "Session FILL (pre-flush) — added %d frames (~%.0fms) to buffer",
                    len(all_pre), len(all_pre) * FRAME_DURATION_MS,
                )
                for frame in all_pre:
                    session.send_audio(frame)
                    audio_buffer.append(frame)

            self._listening = True
            last_speech_time = time.time()
            session_start = time.time()
            # Track index of last frame with speech energy — used to trim
            # trailing silence from the speaker-recognition buffer at session
            # end. SILENCE_TIMEOUT_S holds the session open for ~2.5s after
            # the user stops, so without this the voiceprint ends up 30-50%
            # silence and the embedding degrades.
            last_speech_idx: int = len(audio_buffer) - 1
            # Signal Lumi to show listening LED as soon as mic session opens (before transcript arrives)
            try:
                requests.post("http://127.0.0.1:5000/api/sensing/event",
                              json={"type": "voice_listening", "message": "listening"},
                              timeout=0.3)
            except Exception:
                pass

            while self._running and not session.is_closed():
                # If TTS or music starts mid-session, stop streaming immediately
                if self._tts_is_speaking():
                    logger.info("TTS started mid-session, closing STT to avoid echo")
                    break
                if self._music_is_playing():
                    logger.info("Music started mid-session, closing STT")
                    break

                # Guard against zombie sessions
                if (time.time() - session_start) > MAX_SESSION_DURATION_S:
                    logger.warning("STT session exceeded %ds, force-closing", MAX_SESSION_DURATION_S)
                    break


                data, overflowed = mic.read(frame_size)
                if overflowed:
                    continue

                resampled = self._resample_to_stt(data, device_rate)
                try:
                    session.send_audio(resampled)
                except Exception as e:
                    logger.warning("send_audio failed (connection dead?): %s", e)
                    break
                audio_buffer.append(resampled)

                rms = self._rms(data)
                if rms >= RMS_THRESHOLD:
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
            session.close()
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
                        dropped, dropped * FRAME_DURATION_MS / 1000,
                        len(ser_audio_buffer),
                    )

            # Final snapshot of the buffer for traceability before it goes
            # out of scope. 1 session = 1 speaking turn = this many frames.
            buf_frames = len(audio_buffer)
            buf_bytes = sum(len(b) for b in audio_buffer)
            buf_duration = buf_bytes / (STT_RATE * 2)
            logger.info(
                "Session END — buffer frames=%d bytes=%d duration=%.2fs transcript=%r",
                buf_frames, buf_bytes, buf_duration, combined or "(empty)",
            )
                      
            # Prepare message and user for speaker recognition and SER. 
            from lelamp.service.voice.speech_emotion.constants import UNKNOWN_USER_LABEL
            final_text, event_type = self._resolve_wake_word_split(combined)
            user = UNKNOWN_USER_LABEL
            
            # 1. Speaker recognize and decorate the final text to send to Lumi.
            if combined:
                final_msg, se_user = self._identify_and_decorate(final_text, audio_buffer)
                user = se_user if se_user else UNKNOWN_USER_LABEL
                logger.info("Final message → Lumi (%s): %r", event_type, final_msg)
                self._send_to_lumi(final_msg, event_type=event_type)

            # 2. Submit SER — uses the UNTRIMMED snapshot so laughter / sighs
            self._submit_speech_emotion_from_session(ser_audio_buffer, user=user)
            
            
            # Clear listening LED
            try:
                requests.post("http://127.0.0.1:5000/api/sensing/event",
                              json={"type": "voice_listening_end", "message": "done"},
                              timeout=0.3)
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
                        from lelamp import app_state
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

    def _is_echo(self, transcript: str) -> bool:
        """Check if transcript is echo of last TTS output (Layer 3: transcript self-filter)."""
        if not self._tts or not self._tts.last_spoken_text:
            return False
        # Only relevant within a time window after TTS finished
        elapsed = time.time() - self._tts.last_spoken_time
        if elapsed > ECHO_RELEVANCE_WINDOW_S:
            return False
        from difflib import SequenceMatcher
        similarity = SequenceMatcher(
            None, transcript.lower(), self._tts.last_spoken_text.lower()
        ).ratio()
        if similarity >= ECHO_SIMILARITY_THRESHOLD:
            logger.info(
                "Echo detected (similarity=%.2f): '%s' ≈ TTS:'%s' — dropping",
                similarity, transcript[:60], self._tts.last_spoken_text[:60],
            )
            return True
        return False

    def _send_to_lumi(self, message: str, event_type: str = "voice"):
        """Send the final decorated message (speaker prefix + optional audio
        path) to Lumi as a sensing event.

        ``message`` is already the output of ``_identify_and_decorate`` — it
        contains ``"<Name>: <text>"`` for a known speaker or
        ``"Unknown Speaker:<text> (audio save at <path>)"`` for an unenrolled one.
        """
        # Layer 3: transcript self-filter — drop if it's echo of our own TTS
        if self._is_echo(message):
            return

        import json as _json
        payload = {"type": event_type, "message": message}
        logger.info("curl -s -X POST %s -H 'Content-Type: application/json' -d '%s'",
                    LUMI_SENSING_URL, _json.dumps(payload))
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(
                    LUMI_SENSING_URL,
                    json=payload,
                    timeout=5,
                )
                if resp.status_code == 503 and attempt < max_retries:
                    logger.warning("Lumi agent not ready (503), retrying in 2s... (attempt %d/%d)", attempt, max_retries)
                    time.sleep(2)
                    continue
                elif resp.status_code != 200:
                    logger.warning("Lumi returned %d: %s", resp.status_code, resp.text)
                else:
                    logger.info("Sent to Lumi: %r", message)
                return
            except requests.ConnectionError as e:
                if attempt < max_retries:
                    logger.warning("Lumi not reachable (attempt %d/%d), retrying in 2s...", attempt, max_retries)
                    time.sleep(2)
                else:
                    logger.warning("Failed to send voice event to Lumi after %d attempts: %s", max_retries, e)
            except requests.RequestException as e:
                logger.warning("Failed to send voice event to Lumi: %s", e)
                return
