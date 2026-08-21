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
# Dead-air filler for the realtime wait. os-server owns the phrase pools, the
# language resolution, and the WAV cache; HAL only decides WHEN the wait has run
# long enough to deserve one. See PlayFiller in
# system/server/sensing/delivery/http/deadair_filler.go.
OS_FILLER_URL = "http://127.0.0.1:5000/api/sensing/filler"


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
SILENCE_TIMEOUT_S = float(os.environ.get("HAL_SILENCE_TIMEOUT", "2.5"))
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

# Silero on the SILENCE clock (end of turn), not just the entry gate.
#
# Closing a session is driven by RMS alone: any frame above RMS_THRESHOLD
# refreshes the silence timer. In a noisy room the noise floor sits above the
# threshold, so the timer never expires — the turn runs to MAX_SESSION_DURATION
# and ships mostly room noise to STT, which comes back empty or as junk and the
# device answers nothing (device-observed 18/08/2026: sessions of 8-25s with
# transcript='(empty)'). Energy VAD misses roughly half of real speech frames
# in noise; production voice stacks (Pipecat, LiveKit, Deepgram) all put a
# neural VAD on this decision instead.
#
# So RMS stays as the cheap first gate, and Silero confirms before the timer is
# actually refreshed. Batched over a window rather than run per frame: Silero
# costs ~20ms/frame on ARM and its LSTM wants more than one 64ms frame to
# settle. Set HAL_SILENCE_VAD_ENABLED=false to fall back to pure RMS.
SILENCE_VAD_ENABLED = os.environ.get("HAL_SILENCE_VAD_ENABLED", "true").lower() == "true"
SILENCE_VAD_WINDOW_FRAMES = int(os.environ.get("HAL_SILENCE_VAD_WINDOW_FRAMES", "3"))


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
# Acoustic echo cancellation (WebRTC AEC3) — see drivers/voice/aec.py.
# Off by default: it needs the `aec-audio-processing` native binding, which is
# not a hal dependency. Absent, every AEC entry point is a no-op.
# ---------------------------------------------------------------------------
AEC_ENABLED = os.environ.get("HAL_AEC_ENABLED", "false").lower() == "true"
# Speaker→mic delay hint. AEC3 estimates the real delay itself, but the hint
# decides how fast it converges. Measured on a lamp (USB mic + USB speaker) the
# true lag is ~154ms; correcting the hint from 80 to 150 took ERLE from 10.9 to
# 18.6 dB overall and 6.5 to 14.2 dB during the convergence phase.
AEC_DELAY_MS = int(os.environ.get("HAL_AEC_DELAY_MS", "150"))
AEC_NOISE_SUPPRESSION = os.environ.get("HAL_AEC_NS", "true").lower() == "true"
# Keep cancelling for this long after the last speaker write, then bypass the
# APM until playback resumes.
AEC_TAIL_S = float(os.environ.get("HAL_AEC_TAIL_S", "0.5"))
# Set to a directory to write aec_mic/ref/out.wav for offline ERLE analysis.
AEC_DUMP_DIR = os.environ.get("HAL_AEC_DUMP_DIR", "")


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
_wake_name = _hal_config.resolve_device_type("friend")
WAKE_WORD_PREFIXES = ("hello", "hey", "hi", "alo", "okay", "ok", "wake up")
DEFAULT_WAKE_WORDS = [
    *(f"{prefix} autonomous" for prefix in WAKE_WORD_PREFIXES),
    *(f"{prefix} {_wake_name}" for prefix in WAKE_WORD_PREFIXES),
]


# ---------------------------------------------------------------------------
# Enroll-nudge cooldown
# ---------------------------------------------------------------------------
ENROLL_NUDGE_COOLDOWN_S = float(os.environ.get("HAL_ENROLL_NUDGE_COOLDOWN_S", str(30 * 60)))
