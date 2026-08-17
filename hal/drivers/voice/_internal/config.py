"""Voice service environment-variable configuration.

All HAL_* knobs live here so voice_service.py doesn't need a 60-line
config preamble. Defaults match the previous in-line values.
"""

import os
from pathlib import Path

from hal import config as _hal_config


# ---------------------------------------------------------------------------
# OS server endpoint
# ---------------------------------------------------------------------------
OS_SENSING_URL = "http://127.0.0.1:5000/api/sensing/event"


# ---------------------------------------------------------------------------
# Audio framing — must stay device-rate-independent
# ---------------------------------------------------------------------------
STT_RATE = 16000             # Rate expected by all STT providers
CHANNELS = 1
FRAME_DURATION_MS = 64       # Frame duration in ms


# ---------------------------------------------------------------------------
# Local VAD — RMS energy gate
# ---------------------------------------------------------------------------
RMS_THRESHOLD = int(os.environ.get("HAL_VAD_THRESHOLD", "3500"))
# Silence, in seconds, before the capture loop closes the STT session and the
# turn is committed to the realtime model. This sits ENTIRELY IN FRONT of the
# model's own response time, so it is added to every turn the user waits through:
#
#     user stops talking → SILENCE_TIMEOUT_S → activityEnd → ~1.4s → first audio
#
# Was 2.5s, which made a ~1.4s model response feel like ~3.9s — 64% of the wait
# was this timer. Measured on intern-v2-893f 2026-08-17: dropping it to 1.2s cut
# perceived latency ~33% with no loss of transcript quality over the turns tested
# (every utterance still arrived as ONE final segment, including a sub-second
# reply and a disfluent restart — "what do you think that, uh, what do you think
# about…"). Model-side latency was unchanged at 1437/1541ms, confirming this is
# pure dead-wait removal rather than a trade.
#
# It cannot be tuned by the logs alone: `[realtime] Response latency` stamps
# _speech_ended_at on each audio send, so its anchor lands AFTER this timer
# expires and the figure never moves when this value changes. Judge it by feel.
#
# The floor is set by Deepgram Flux, which fires EndOfTurn on natural
# mid-sentence pauses; HAL deliberately ignores those and waits for this timer
# instead (see voice_service.on_transcript) so one sentence is not split across
# turns. Too low and a thinking pause gets answered mid-thought; raise it if
# users report being cut off.
SILENCE_TIMEOUT_S = float(os.environ.get("HAL_SILENCE_TIMEOUT", "1.2"))
SPEECH_HOLDOFF_S = float(os.environ.get("HAL_SPEECH_HOLDOFF", "0.2"))
# Pre-roll lookback — 8 × 64ms = 512ms of audio history before VAD trigger so
# quiet first syllables ("b", "k", "t", "p") reach STT instead of getting clipped.
PRE_ROLL_FRAMES = int(os.environ.get("HAL_PRE_ROLL_FRAMES", "8"))
SESSION_COOLDOWN_S = float(os.environ.get("HAL_SESSION_COOLDOWN_S", "0.3"))


# ---------------------------------------------------------------------------
# Silero VAD (semantic, ONNX) — rejects TV/music/non-speech audio
# ---------------------------------------------------------------------------
SILERO_VAD_ENABLED = os.environ.get("HAL_SILERO_ENABLED", "false").lower() == "true"
SILERO_VAD_THRESHOLD = float(os.environ.get("HAL_SILERO_THRESHOLD", "0.3"))
SILERO_CHUNK_SIZE = int(os.environ.get("HAL_SILERO_CHUNK_SIZE", "512"))
SILERO_MODEL_PATH = Path(__file__).resolve().parent.parent / "resources" / "silero_vad.onnx"


# ---------------------------------------------------------------------------
# WebRTC VAD — fast C-based pre-filter (~0.1ms vs Silero ~20ms)
# ---------------------------------------------------------------------------
WEBRTCVAD_ENABLED = os.environ.get("HAL_WEBRTCVAD_ENABLED", "false").lower() == "true"
WEBRTCVAD_AGGRESSIVENESS = int(os.environ.get("HAL_WEBRTCVAD_AGGRESSIVENESS", "2"))
WEBRTCVAD_FRAME_MS = int(os.environ.get("HAL_WEBRTCVAD_FRAME_MS", "30"))


# ---------------------------------------------------------------------------
# Echo handling — adaptive RMS gate after TTS + transcript similarity filter
# ---------------------------------------------------------------------------
ECHO_RMS_FLOOR = int(os.environ.get("HAL_ECHO_RMS_FLOOR", "200"))
ECHO_GATE_MAX_WAIT_S = float(os.environ.get("HAL_ECHO_GATE_MAX_WAIT_S", "1.5"))
ECHO_GATE_WINDOW_S = float(os.environ.get("HAL_ECHO_GATE_WINDOW_S", "0.05"))
ECHO_SIMILARITY_THRESHOLD = float(os.environ.get("HAL_ECHO_SIMILARITY_THRESHOLD", "0.55"))
ECHO_RELEVANCE_WINDOW_S = float(os.environ.get("HAL_ECHO_RELEVANCE_WINDOW_S", "15.0"))
MAX_SESSION_DURATION_S = float(os.environ.get("HAL_MAX_SESSION_DURATION_S", "30"))

# Warm mic — keep the arecord capture stream OPEN across TTS/music (drain +
# discard frames) instead of closing it and paying a cold arecord reopen
# (~1s on slow USB mics) on the next turn. That reopen latency is dead air
# right after a push-to-talk cue ("listening!"), so the user's first words
# land before the mic is live and get clipped. Default off → legacy behavior
# (close on TTS, reopen after). Opt in with HAL_WARM_MIC=true.
WARM_MIC = os.environ.get("HAL_WARM_MIC", "false").lower() == "true"
# Max echo-skip after TTS/music ends before resuming VAD (warm mic only).
# Bounded ≪ the legacy 1.5s reverb gate so a user who talks right after a cue
# resumes fast and the pre-roll lookback captures their opening words. Skips
# early once the room drops below ECHO_RMS_FLOOR.
WARM_MIC_ECHO_SKIP_MAX_S = float(os.environ.get("HAL_WARM_MIC_ECHO_SKIP_MAX_S", "0.3"))


# ---------------------------------------------------------------------------
# STT keepalive — pre-connect WS before speech is detected to cut latency
# ---------------------------------------------------------------------------
STT_KEEPALIVE = os.environ.get("HAL_STT_KEEPALIVE", "false").lower() == "true"
# Send a KeepAlive every N seconds while pre-connected and idle, so the server
# doesn't idle-close the WS (~10s) and force a slow cold-reconnect at speech start
# (the cause of empty transcripts on short/quiet utterances). Must be < server
# idle timeout.
STT_KEEPALIVE_PING_S = float(os.environ.get("HAL_STT_KEEPALIVE_PING_S", "3"))


# ---------------------------------------------------------------------------
# Voice barge-in — interrupt in-flight TTS when user speaks during playback.
# Requires hardware where mic doesn't pick up speaker bleed above the
# threshold (physical separation or hardware AEC). Default off; enable only
# after measuring bleed RMS at the deployed mic position.
#
# BLOCK_MS sizes the per-read chunk of the monitor's mic capture. Larger
# blocks = fewer Python wakeups + fewer numpy passes, which is critical on
# Pi-class boards where the TTS sounddevice pump is already CPU-bound.
# 256ms gives roughly 4x less per-frame overhead vs the 64ms VAD frame size
# at the cost of trigger latency (1 block = 256ms minimum response time).
# ---------------------------------------------------------------------------
BARGE_IN_ENABLED = os.environ.get("HAL_BARGE_IN_ENABLED", "false").lower() == "true"
BARGE_IN_RMS_THRESHOLD = int(os.environ.get("HAL_BARGE_IN_RMS_THRESHOLD", "9000"))
BARGE_IN_TRIGGER_FRAMES = int(os.environ.get("HAL_BARGE_IN_TRIGGER_FRAMES", "1"))
BARGE_IN_BLOCK_MS = int(os.environ.get("HAL_BARGE_IN_BLOCK_MS", "256"))


# ---------------------------------------------------------------------------
# Speaker recognition — prefix every transcript with "<Name>: "
# ---------------------------------------------------------------------------
SPEAKER_RECOGNITION_ENABLED = _hal_config.SPEAKER_RECOGNITION_ENABLED
SPEAKER_MIN_AUDIO_S = _hal_config.SPEAKER_MIN_AUDIO_S
SPEECH_EMOTION_ENABLED = _hal_config.SPEECH_EMOTION_ENABLED


# ---------------------------------------------------------------------------
# Wake words — fallback derived from the device type (lamp/dog/intern) when no
# IDENTITY.md name is set, so an unnamed device isn't hardcoded to "lamp".
# Last resort "friend" when device_type is also unavailable.
# ---------------------------------------------------------------------------
_wake_name = (_hal_config._os_cfg_get("device_type") or "friend").strip().lower()
WAKE_WORD_PREFIXES = ("hello", "hey", "hi", "alo", "okay", "ok", "wake up")
DEFAULT_WAKE_WORDS = [
    *(f"{prefix} autonomous" for prefix in WAKE_WORD_PREFIXES),
    *(f"{prefix} {_wake_name}" for prefix in WAKE_WORD_PREFIXES),
]


# ---------------------------------------------------------------------------
# Enroll-nudge cooldown
# ---------------------------------------------------------------------------
ENROLL_NUDGE_COOLDOWN_S = float(os.environ.get("HAL_ENROLL_NUDGE_COOLDOWN_S", str(30 * 60)))
