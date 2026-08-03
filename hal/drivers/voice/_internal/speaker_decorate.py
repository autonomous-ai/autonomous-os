"""Post-STT transcript decoration.

Wraps three closely-related concerns that all run after STT produces a final
transcript:

  1. Wake-word resolution    — strip "hey <name>" prefix, classify event type
  2. Speaker identification  — prefix "<Name>: " from voice embedding
  3. Speech-emotion submit   — async SER call on the full mic session

All speaker-recog + SER state lives here so VoiceService doesn't carry it.
"""

import logging
import os
import re
import threading
import time
from typing import Optional

from hal.drivers.voice._internal.config import (
    SPEAKER_MIN_AUDIO_S,
    SPEAKER_RECOGNITION_ENABLED,
    SPEECH_EMOTION_ENABLED,
    STT_RATE,
)

logger = logging.getLogger("hal.voice")


def merge_wake_words(*word_lists: list[str]) -> list[str]:
    """Merge wake-word aliases case-insensitively while preserving order."""
    merged: list[str] = []
    seen: set[str] = set()
    for words in word_lists:
        for word in words:
            normalized = word.strip().casefold()
            if normalized and normalized not in seen:
                seen.add(normalized)
                merged.append(normalized)
    return merged


def merge_stt_hypothesis(previous: str, current: str) -> str:
    """Merge cumulative and delta-style STT transcript updates.

    Providers do not agree on interim semantics. One may send ``Hello`` then
    ``Hello Luna``; another may send ``Hello`` then only the new token
    ``Luna``. The wake-word gate needs a single leading hypothesis for both
    shapes, without waiting for end-of-speech.
    """
    previous_words = re.findall(r"\w+", previous.casefold())
    current_words = re.findall(r"\w+", current.casefold())
    if not previous_words:
        return " ".join(current_words)
    if not current_words:
        return " ".join(previous_words)
    if current_words[:len(previous_words)] == previous_words:
        return " ".join(current_words)
    if previous_words[:len(current_words)] == current_words:
        return " ".join(current_words)

    # Delta-style updates can repeat their boundary word ("hello luna" then
    # "luna what time is it"). Keep the longest shared suffix/prefix once.
    overlap = min(len(previous_words), len(current_words))
    while overlap and previous_words[-overlap:] != current_words[:overlap]:
        overlap -= 1
    return " ".join(previous_words + current_words[overlap:])


class SpeakerDecorator:
    """Owns wake-word list + speaker recognizer + speech-emotion service."""

    def __init__(self, wake_words: list, nudge_cooldown_s: float, enable_people_perception: bool = True):
        self._wake_words: list = list(wake_words)
        self._wake_words_lock = threading.Lock()

        # Enroll-nudge cooldown per voiceprint_hash. In-memory only — resets on
        # restart (acceptable; worst case is one extra prompt after reboot).
        self._last_nudge_time: dict[str, float] = {}
        self._nudge_cooldown_s: float = nudge_cooldown_s

        self._speaker = self._init_speaker(enable_people_perception)
        self._speech_emotion = self._init_speech_emotion(enable_people_perception)

    # ------------------------------------------------------------------
    # Lazy service init
    # ------------------------------------------------------------------
    @staticmethod
    def _init_speaker(enable_people_perception: bool = True):
        # Speaker recognition (identifying WHO is speaking from their voiceprint)
        # is voice people-perception — gated on the `audio` capability (the mic).
        # It needs only a mic, so any device that declares `audio` runs it.
        if not enable_people_perception:
            logger.info("Speaker recognition off — device does not declare 'audio' (no mic for voice people-perception)")
            return None
        if not SPEAKER_RECOGNITION_ENABLED:
            logger.info(
                "Speaker recognizer disabled by HAL_SPEAKER_RECOGNITION_ENABLED=false "
                "(default is true — this is an explicit opt-out).",
            )
            return None
        try:
            from hal.drivers.voice.speaker_recognizer import SpeakerRecognizer
            recognizer = SpeakerRecognizer()
            if not recognizer.available:
                logger.info(
                    "Speaker recognizer idle — SPEAKER_EMBEDDING_API_URL not set "
                    "(service instance exists but embedding calls will return 'unknown' with an error)",
                )
            else:
                logger.info("Speaker recognizer enabled — will prefix every STT final with speaker name")
            return recognizer
        except Exception as e:
            logger.warning("Speaker recognizer init failed: %s", e)
            return None

    @staticmethod
    def _init_speech_emotion(enable_people_perception: bool = True):
        # Speech emotion (reading the user's emotion from voice) is voice
        # people-perception — gated on the `audio` capability (the mic), not the
        # camera. Any device with a mic runs it; it is not a hard requirement.
        if not enable_people_perception:
            logger.info("Speech emotion recognition off — device does not declare 'audio' (no mic for voice people-perception)")
            return None
        if not SPEECH_EMOTION_ENABLED:
            logger.info("Speech emotion recognition disabled by HAL_SPEECH_EMOTION_ENABLED=false")
            return None
        try:
            from hal.drivers.voice.speech_emotion import SpeechEmotionService
            service = SpeechEmotionService()
            if not service.available:
                logger.info("Speech emotion service idle — DL backend URL not set")
            else:
                logger.info("Speech emotion service enabled")
            return service
        except Exception as e:
            logger.warning("Speech emotion service init failed: %s", e)
            return None

    # ------------------------------------------------------------------
    # Wake-word management
    # ------------------------------------------------------------------
    def set_wake_words(self, words: list) -> None:
        """Update wake word list at runtime (called when agent is renamed)."""
        with self._wake_words_lock:
            self._wake_words = [w.lower() for w in words]
        logger.info("Wake words updated: %s", self._wake_words)

    def starts_with_wake_word(self, transcript: str) -> bool:
        """Return true when an interim transcript starts with a wake phrase.

        The realtime gate must not trigger merely because the agent name appears
        in the middle of a conversation. Case and punctuation are ignored so
        Deepgram variants such as ``Hey Luna, ...`` and ``hey luna ...`` match.
        """
        normalized = re.sub(r"[^\w\s]", "", transcript.casefold()).strip()
        if not normalized:
            return False
        with self._wake_words_lock:
            wake_words = list(self._wake_words)
        for wake_word in wake_words:
            phrase = re.sub(r"[^\w\s]", "", wake_word.casefold()).strip()
            if phrase and (normalized == phrase or normalized.startswith(phrase + " ")):
                return True
        return False

    def resolve_wake_word_split(self, combined: str) -> tuple[str, str]:
        """Detect wake word in `combined` and split it off.

        Returns (final_text, event_type):
          * final_text — text sent to the OS server (wake word stripped on command).
          * event_type — "voice_command" if a wake word matched at the start,
                         else "voice".

        Empty combined → ("", "voice"); caller typically skips the POST then.
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

    # ------------------------------------------------------------------
    # Speaker identification
    # ------------------------------------------------------------------
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
        """Format OS server message for an unrecognized speaker (enroll hints, cooldown)."""
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

    def identify_and_decorate(
        self, transcript: str, audio_buffer: list[bytes],
    ) -> tuple[str, Optional[str], Optional[str]]:
        """Run speaker recognition; return (OS server message, SER user, display).

        - OS server message — transcript decorated with the speaker prefix.
        - SER user — known label or "unknown" (only when recognize completes
          without `error`); None skips SER.
        - display — the matched speaker's display name (e.g. "Darren") on a
          confident match, else None. Used to name the voice speaker in the
          realtime turn context WITHOUT re-running recognition; None on
          unknown / STOI-reject / server error so the caller falls back cleanly.
        """
        logger.info("Identify and decorate transcript: raw transcript is: '%s'", transcript)
        if self._speaker is None:
            logger.info(
                "Skip speaker ID: recognizer not initialized "
                "(HAL_SPEAKER_RECOGNITION_ENABLED or init failure)",
            )
            return transcript, None, None
        if not audio_buffer:
            logger.warning("Skip speaker ID: audio buffer is empty (no frames captured this session)")
            return transcript, None, None
        try:
            from hal.drivers.voice.speech_emotion.constants import UNKNOWN_USER_LABEL
            from hal.drivers.voice.speaker_recognizer.speaker_recognizer import pcm16_bytes_to_wav
        except Exception as e:
            logger.warning("Skip speaker ID: helper import failed: %s", e)
            return transcript, None, None

        total_bytes = sum(len(b) for b in audio_buffer)
        duration_s = total_bytes / (STT_RATE * 2)  # int16 mono
        if duration_s < SPEAKER_MIN_AUDIO_S:
            logger.info(
                "Skip speaker ID: only %.2fs of audio buffered (<%.2fs)",
                duration_s, SPEAKER_MIN_AUDIO_S,
            )
            return transcript, None, None

        try:
            wav_bytes = pcm16_bytes_to_wav(b"".join(audio_buffer), STT_RATE)
            import base64 as _b64
            audio_b64 = _b64.b64encode(wav_bytes).decode("ascii")
            result = self._speaker.recognize(audio_b64, source_type="base64")
        except Exception as e:
            logger.warning("Speaker recognize failed: %s", e)
            return transcript, None, None

        logger.info("Speaker recognize result: %r", result)
        err = result.get("error")
        audio_path = result.get("unknown_audio_path", "")
        vp_hash = result.get("voiceprint_hash")
        if err:
            logger.warning("Speaker ID skipped — embedding server issue: %s", err)
            if audio_path:
                return self._format_unknown_speaker_message(
                    transcript, audio_path, duration_s, vp_hash,
                ), None, None
            return transcript, None, None

        name = result.get("name", "unknown")
        confidence = result.get("confidence", 0.0)
        if result.get("match") and name and name != "unknown":
            display = result.get("display_name") or name.capitalize()
            logger.info(
                "Speaker ID: %s (confidence=%.2f, audio=%s)",
                name, confidence, audio_path or "-",
            )
            return f"Speaker - {display}: {transcript}", name, display

        logger.info(
            "Speaker ID: unknown (best=%.2f, audio=%s, hash=%s)",
            confidence, audio_path or "-", vp_hash or "-",
        )
        return self._format_unknown_speaker_message(
            transcript, audio_path, duration_s, vp_hash,
        ), UNKNOWN_USER_LABEL, None

    # ------------------------------------------------------------------
    # Speech-emotion submission
    # ------------------------------------------------------------------
    @staticmethod
    def _session_wav_for_ser(audio_buffer: list[bytes]) -> Optional[tuple[bytes, float]]:
        """Build mono 16 kHz WAV + duration from the STT session buffer (for SER)."""
        if not audio_buffer:
            return None
        duration_s = sum(len(b) for b in audio_buffer) / (STT_RATE * 2)
        if duration_s < SPEAKER_MIN_AUDIO_S:
            return None
        try:
            from hal.drivers.voice.speaker_recognizer.speaker_recognizer import (
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

    def submit_speech_emotion_from_session(
        self, audio_buffer: list[bytes], user: str = "unknown",
    ) -> None:
        """Submit SER on the full mic-session buffer (async via the service).

        A turn ignored by the wake-word gate does not run speaker
        identification, so its SER record intentionally uses ``unknown``.
        """
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
