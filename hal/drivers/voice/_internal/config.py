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
WARM_MIC = os.environ.get("HAL_WARM_MIC", "true").lower() == "true"
# Max echo-skip after TTS/music ends before resuming VAD (warm mic only).
# Bounded ≪ the legacy 1.5s reverb gate so a user who talks right after a cue
# resumes fast and the pre-roll lookback captures their opening words. Skips
# early once the room drops below ECHO_RMS_FLOOR.
WARM_MIC_ECHO_SKIP_MAX_S = float(os.environ.get("HAL_WARM_MIC_ECHO_SKIP_MAX_S", "0.1"))


# ---------------------------------------------------------------------------
# Acoustic echo cancellation (WebRTC AEC3) — see drivers/voice/aec.py.
# On by default. It needs the `aec-audio-processing` native binding, which is
# not a hal dependency — absent, every AEC entry point degrades to a no-op and
# the voice path behaves exactly as it did before, so defaulting this on cannot
# break a device that lacks the binding. Note that it also turns BARGE_IN on
# (see BARGE_IN_ENABLED below), which is NOT a no-op.
# ---------------------------------------------------------------------------
AEC_ENABLED = os.environ.get("HAL_AEC_ENABLED", "false").lower() == "true"
# Speaker→mic delay hint. AEC3 estimates the real delay itself, but the hint
# decides how fast it converges. Measured from HAL's own aec_mic/aec_ref dump
# (24/08/2026, lamp-ee17): the lag between the frames the APM is actually
# handed is 204ms median, not the 150 this used to declare. It also drifts
# 154→215ms over 93s (~667ppm) because the mic and speaker are separate USB
# devices with independent clocks — which is why this is a per-device value and
# not a constant. Re-measure with HAL_AEC_DUMP_DIR after any audio hardware
# change; a wrong hint costs convergence, not correctness.
AEC_DELAY_MS = int(os.environ.get("HAL_AEC_DELAY_MS", "205"))
AEC_NOISE_SUPPRESSION = os.environ.get("HAL_AEC_NS", "true").lower() == "true"
# Keep cancelling for this long after the last speaker write, then bypass the
# APM until playback resumes. 2.0, not 0.5: the gaps between the sentences of
# one reply are longer than half a second, so a 0.5s tail let the canceller
# bypass and reset mid-reply and then re-converge from cold. Every bypass costs
# convergence — the ERLE logged in the window right after `AEC engaged` ranges
# from -25.1 dB to 23.2 dB, against 15-23 dB once settled.
AEC_TAIL_S = float(os.environ.get("HAL_AEC_TAIL_S", "2.0"))
# Depth of the echo-reference FIFO. It must hold everything written but
# not yet heard: the tap fires when ALSA ACCEPTS audio, the mic hears it a
# full output buffer later, and TTS writes in network-paced bursts. When
# the FIFO is shallower than that lead, the oldest bytes -- exactly the
# ones the mic is about to hear -- are dropped and the reference then runs
# dry for the rest of the burst. Measured on lamp-ee17 at 500ms: the
# reference underran on 30-98% of processed frames during a reply.
AEC_REF_MS = int(os.environ.get("HAL_AEC_REF_MS", "500"))
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
# Speaker-ID prepass — how long the turn may wait for it before committing
# ---------------------------------------------------------------------------
# The prepass is an external embedding call (measured 1.49s on lamp-0c89,
# 03/09/2026) and it used to run STRICTLY BEFORE the realtime turn opened, so
# its whole round trip sat in front of the Gemini connect and the audio flush —
# dead time between the user finishing a sentence and the model hearing it. It
# now runs on its own thread while that connect happens, and the turn joins it
# here, just before the point where the speaker's name is actually needed.
#
# The wait is a ceiling, not a delay: a prepass that finished during the connect
# costs nothing. Reaching the ceiling only means this turn's [TURN CONTEXT] goes
# out with the speaker unresolved — the same thing the always-listening path has
# always done, and the late-correction path already covers it.
SPEAKER_PREPASS_JOIN_S = float(os.environ.get("HAL_SPEAKER_PREPASS_JOIN_S", "2.0"))

# How long a resolved speaker identity is reused instead of re-running the
# recognizer. The prepass is an external inference call on every turn — a
# conversation of ten turns paid for ten of them to be told the same name, while
# each call adds ~1.4s in front of the model hearing the audio (lamp-0c89,
# 03/09/2026). Voices do not change mid-conversation; the cache is what a face
# identity already gets by aging out rather than being re-derived per frame.
#
# UNKNOWN is cached too, and deliberately: an utterance the recognizer could not
# place is the case most likely to repeat (a guest, a bad angle, a short clip),
# and retrying it every turn pays the full latency for the same non-answer.
SPEAKER_ID_CACHE_S = float(os.environ.get("HAL_SPEAKER_ID_CACHE_S", "90"))
# Inside a wake-word follow-up window the turns are one conversation by
# definition, so the cache holds for the whole window regardless of the TTL
# above. 0 disables the extension.
SPEAKER_ID_CACHE_FOLLOWUP_S = float(
    os.environ.get("HAL_SPEAKER_ID_CACHE_FOLLOWUP_S", "300")
)


# ---------------------------------------------------------------------------
# Voice barge-in — interrupt in-flight TTS when the user speaks during playback.
# Defaults to AEC_ENABLED: cancellation is what makes it safe, so the two turn
# on together.
#
# EVERY NUMBER BELOW IS PER-DEVICE. They are the values running on lamp-ee17
# (USB mic card 3 + USB speaker card 4, independent clocks, speaker at 25%) and
# they are a starting point, not a recipe. Re-measure on new audio hardware:
# park HAL_BARGE_IN_RMS_THRESHOLD at 30000 so nothing can fire, speak nothing,
# and read the `drain peak RMS=` line each reply logs.
#
# The honest state of this gate, measured 25/08/2026 with AEC_DELAY_MS=205:
#   echo residual ceiling (room silent, 3 replies)   9804  6510  7849
#   real user interruptions                          8027 (and 9361-20298 on
#                                                    24/08 at the old 150 hint)
# The populations OVERLAP — 8027 sits under the 9804 ceiling — so no level
# threshold separates them. A threshold below the ceiling self-interrupts; one
# above it misses quiet interruptions. Re-measured at three speaker volumes on
# 27/08/2026 (see BARGE_IN_ECHO_MATCH): they overlap at all three, and lowering
# the volume does not pull the echo down. What separates them is the envelope
# test in BARGE_IN_ECHO_MATCH, which the level gate now runs ahead of.
#
# BLOCK_MS sizes the per-read chunk of the LEGACY monitor's mic capture, which
# is unreachable while WARM_MIC is true (the default) — the warm path reuses the
# capture loop's own 64ms frames. It is kept aligned with that frame size so the
# two paths trigger on comparable evidence if warm mic is ever turned off.
# ---------------------------------------------------------------------------
BARGE_IN_ENABLED = os.environ.get(
    "HAL_BARGE_IN_ENABLED", str(AEC_ENABLED)
).lower() == "true"
# 5000. This is BELOW the measured echo ceiling above, deliberately: it favours
# catching a normal speaking voice over never self-interrupting. Expect the lamp
# to cut itself off on the loudest syllables of its own replies — at 4500 that
# was observed firing on RMS 5530 / 6446 / 6637 / 7749, twice transcribing the
# lamp's own words as the user's turn. Raise toward 11000 to trade the other
# way; no value of THIS gate alone gets both — BARGE_IN_ECHO_MATCH is what
# rejects the echo that survives it, so leave this low enough to catch a normal
# voice and let the envelope test do the separating.
BARGE_IN_RMS_THRESHOLD = int(os.environ.get("HAL_BARGE_IN_RMS_THRESHOLD", "5000"))
BARGE_IN_TRIGGER_FRAMES = int(os.environ.get("HAL_BARGE_IN_TRIGGER_FRAMES", "1"))
BARGE_IN_BLOCK_MS = int(os.environ.get("HAL_BARGE_IN_BLOCK_MS", "64"))
# Warm-mic path only: consecutive 64ms VAD frames over threshold needed to fire.
# BLOCK_MS above sizes the legacy monitor's own reads; the warm path reuses the
# capture loop's frames, so the guard is expressed in frames instead.
BARGE_IN_WARM_FRAMES = int(os.environ.get("HAL_BARGE_IN_WARM_FRAMES", "2"))
# Second condition: the burst must also BE speech. Level alone cannot tell a
# person from a door slam — measured on real recorded echo, webrtcvad accepted
# 7/7 non-speech probes (door slam, keys, cough, beep, music, noise burst,
# chair scrape) at the same level where speech fires, while Silero rejected all
# 7 at no latency cost. Evaluated only after the level gate passes, so the
# model runs on candidates rather than on every drained frame.
# The ratio is below the turn-level noise guard's on purpose: the window is
# short and the audio is post-AEC double talk, which scores lower than a clean
# utterance. Every decision logs its ratio — tune from those.
#
# Do NOT expect this to reject the lamp's own voice; it cannot, because echo IS
# speech. Measured 25/08/2026: echo scored 0.50, 0.75 and 1.00 on separate
# events while real interruptions scored 0.08, 0.88 and 1.00. Raising it to 0.60
# was tried and rejected real turns without stopping the echo. Level does the
# separating; this only rejects loud NON-speech (door slam, keys, cough).
BARGE_IN_REQUIRE_SPEECH = os.environ.get(
    "HAL_BARGE_IN_REQUIRE_SPEECH", "true"
).lower() == "true"
BARGE_IN_SPEECH_RATIO = float(os.environ.get("HAL_BARGE_IN_SPEECH_RATIO", "0.35"))
BARGE_IN_SPEECH_FRAMES = int(os.environ.get("HAL_BARGE_IN_SPEECH_FRAMES", "6"))
# Third condition, and the only one that can tell the lamp's voice from a
# person's: reject the candidate when its loudness envelope tracks what the
# speaker is playing. See aec.echo_envelope_match.
#
# This exists because level cannot do it and re-measuring will not fix it.
# Silent-room echo ceiling vs real interruptions, same room, same reply:
#   speaker 25% (lamp-ee17)   echo 9804   person 8027
#   speaker 40% (lamp-0c89)   echo 9969   person 6956-8027
#   speaker 65% (lamp-0c89)   echo 13560  person 6956
# The echo ceiling sits ABOVE the person at every volume, and 24dB of mixer
# range moved it by under 3dB — so there is no volume to run at and no
# threshold to pick. Turning the speaker down is not a workaround.
#
# Labelled on lamp-0c89 27/08/2026, live, speaker at 40%. The underlying
# measurement is the residual SKEW in dB (see aec._SKEW_ECHO_FLOOR_DB); this
# threshold is that number after mapping:
#   echo, silent room (15 windows)   -2.8 .. +2.1 dB   -> scores 1.00
#   echo, mixed run (~40 windows)   -50.0 .. +4.8 dB   -> scores 1.00
#   confirmed real interruption      +8.4 .. +40.4 dB  -> scores 0.07-0.47
# 0.65 sits in the middle of that gap; a silent-room run of 12 replies fired
# zero barge-ins with it. It is NOT per-device: a normalised
# correlation has no units, so mic gain, speaker level and room all cancel out
# — unlike RMS_THRESHOLD and AEC_DELAY_MS above, which do have to be measured
# per body. Set to 0 to disable the test (the pre-27/08 behaviour).
BARGE_IN_ECHO_MATCH = float(os.environ.get("HAL_BARGE_IN_ECHO_MATCH", "0.65"))
# Pre-roll kept while draining, in 64ms frames. Much longer than PRE_ROLL_FRAMES
# (8 = 512ms) because interrupting the lamp is detected LATE: the level gate
# waits for the person to out-shout the echo, frames arriving without
# cancellation are skipped entirely, and the envelope test only then gets to
# judge. Everything before that moment exists only in this buffer.
# It is bounded on BOTH sides, which is why it is a separate number and not
# simply "as much as possible". Device-observed 27/08/2026:
#   8 frames (512ms)  -> "What do people there usually do?" reached STT as
#                        'usually do.' — the session started mid-phrase.
#   24 frames (1536ms) -> the pre-roll reached back past the interruption into
#                        the reply's own echo and STT transcribed the LAMP:
#                        'with your friends. Why do you think they like doing?'
#   12 frames (768ms)  -> 'Are there any laws in the US?' arrived whole.
# Detecting the person's exact onset instead was tried and removed: aligning a
# 1.5s window against the reference is too unreliable to find the boundary, and
# it returned "no answer" on every candidate on device.
# Only bounds what a barge-in turn may prepend; ordinary turns are still
# trimmed back to PRE_ROLL_FRAMES so their leading silence is unchanged.
BARGE_IN_PRE_ROLL_FRAMES = int(
    os.environ.get("HAL_BARGE_IN_PRE_ROLL_FRAMES", "12")
)


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
