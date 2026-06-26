"""
HAL runtime configuration — all values read from environment variables.

Import: from hal.config import DEVICE_ID, SERVO_PORT, ...
"""

import os
import tempfile
from pathlib import Path
from typing import Optional, Union

# --- Hardware ---
SERVO_PORT = os.environ.get("HAL_SERVO_PORT", "/dev/ttyACM0")
DEVICE_ID = os.environ.get("HAL_DEVICE_ID", "hal")
SERVO_FPS = int(os.environ.get("HAL_SERVO_FPS", "30"))
SERVO_HOLD_S = float(os.environ.get("HAL_SERVO_HOLD_S", "3.0"))
HTTP_PORT = int(os.environ.get("HAL_HTTP_PORT", "5001"))
# production (default): bind 127.0.0.1, local-only middleware enforced.
# developer: bind 0.0.0.0, no access restrictions — for local dev/testing only.
_mode = os.environ.get("HAL_MODE", "production").strip().lower()
MODE: str = "developer" if _mode == "developer" else "production"
HTTP_HOST: str = "0.0.0.0" if MODE == "developer" else "127.0.0.1"
CAMERA_INDEX = int(os.environ.get("HAL_CAMERA_INDEX", "0"))
CAMERA_WIDTH = int(os.environ.get("HAL_CAMERA_WIDTH", "640"))
CAMERA_HEIGHT = int(os.environ.get("HAL_CAMERA_HEIGHT", "480"))
# Camera exposure. Defaults to MANUAL (exposure=500 / gain=255, baked in below)
# so auto-exposure can't throttle the frame rate: UVC auto-exposure stretches
# integration time in low light (~60ms), capping delivery at ~16fps at EVERY
# resolution (not a bandwidth limit). The manual defaults give a bright image at
# ~20fps in a dim room; in a bright room a fixed 50ms exposure can overexpose —
# set HAL_CAMERA_AUTO_EXPOSURE=auto to restore the camera's adaptive auto-exposure
# (brighter/adaptive but throttles fps in low light), or tune the values below.
# exposure_absolute is V4L2 ×100µs: 200=20ms (30fps), 330=33ms (≈30fps), 500=50ms.
CAMERA_AUTO_EXPOSURE = os.environ.get("HAL_CAMERA_AUTO_EXPOSURE", "manual").strip().lower()
CAMERA_EXPOSURE = int(os.environ.get("HAL_CAMERA_EXPOSURE", "500"))
# Sensor gain (camera-specific range, e.g. 0–255). Brightens without costing fps
# but adds noise. Applied in manual mode.
CAMERA_GAIN = int(os.environ.get("HAL_CAMERA_GAIN", "255"))
# Optional brightness offset (camera-specific, e.g. -64..64); unset = camera default.
CAMERA_BRIGHTNESS = int(os.environ["HAL_CAMERA_BRIGHTNESS"]) if os.environ.get("HAL_CAMERA_BRIGHTNESS") else None

# --- Audio ---
# Hardware overrides — set in .env to bypass auto-detection
# e.g. HAL_AUDIO_INPUT_ALSA=plughw:1,0  HAL_AUDIO_OUTPUT_ALSA=plughw:2,0
AUDIO_INPUT_ALSA: Optional[str] = os.environ.get("HAL_AUDIO_INPUT_ALSA") or None
AUDIO_OUTPUT_ALSA: Optional[str] = os.environ.get("HAL_AUDIO_OUTPUT_ALSA") or None
# Separate mic device for SoundPerception (noise sensing).
# Accepts int (sounddevice index) or string (ALSA device name like "plughw:6,0").
_sensing_device_env = os.environ.get("HAL_AUDIO_SENSING_DEVICE")
AUDIO_SENSING_DEVICE: Optional[Union[int, str]] = None
if _sensing_device_env:
    try:
        AUDIO_SENSING_DEVICE = int(_sensing_device_env)
    except ValueError:
        AUDIO_SENSING_DEVICE = _sensing_device_env
# TTS speed multiplier — 1.0=normal, 1.3=faster, max 4.0
TTS_SPEED: float = float(os.environ.get("HAL_TTS_SPEED", "1.3"))
# TTS voice — one of: alloy, ash, coral, echo, fable, onyx, nova, sage, shimmer
TTS_VOICE: str = os.environ.get("TTS_VOICE", "nova")
# TTS instructions — style/vibe prompt for voice (e.g. "Speak warmly like a caring friend")
TTS_INSTRUCTIONS: str = os.environ.get("HAL_TTS_INSTRUCTIONS", "Friendly")
# Stream ElevenLabs TTS over WebSocket (stream-input) instead of HTTP chunked
# streaming. Default off → the unchanged HTTP path. Only affects the elevenlabs
# provider; OpenAI is HTTP-only. Opt in with HAL_TTS_ELEVENLABS_WS=true.
TTS_ELEVENLABS_WS: bool = os.environ.get("HAL_TTS_ELEVENLABS_WS", "false").lower() in ("1", "true", "yes")

# --- Vision tracking ---
# Use the local YOLOv8n model for COCO-class targets (person, cup, etc.).
# Set HAL_TRACKING_DETECT_LOCAL=false to force remote YOLOWorld for everything
# (slower, but open vocabulary and lighter on the Pi CPU).
TRACKING_DETECT_LOCAL_ENABLED: bool = os.environ.get(
    "HAL_TRACKING_DETECT_LOCAL", "true"
).strip().lower() in ("1", "true", "yes", "on")

# Use the local YuNet face detector for target='face' (COCO has no face class,
# YOLO falls back to remote YOLOWorld ~1.3s otherwise). Disable to force remote.
TRACKING_FACE_DETECTOR_ENABLED: bool = os.environ.get(
    "HAL_TRACKING_FACE_DETECTOR", "true"
).strip().lower() in ("1", "true", "yes", "on")

# --- Data layout ---

# --- Sensing: os-server integration ---
OS_SENSING_URL = "http://127.0.0.1:5000/api/sensing/event"
OS_WELLBEING_LOG_URL = "http://127.0.0.1:5000/api/wellbeing/log"
GUARD_STATUS_URL = "http://127.0.0.1:5000/api/guard"
GUARD_CHECK_INTERVAL_S = float(os.environ.get("HAL_GUARD_CHECK_INTERVAL_S", "10.0"))

# --- Sensing: Event cooldown ---
EVENT_COOLDOWN_S = float(os.environ.get("HAL_EVENT_COOLDOWN_S", "60.0"))

# --- Sensing: Sound detection ---
SOUND_RMS_THRESHOLD = int(os.environ.get("HAL_SOUND_RMS_THRESHOLD", "8000"))
SOUND_SAMPLE_DURATION_S = float(os.environ.get("HAL_SOUND_SAMPLE_DURATION_S", "0.5"))

# --- Sensing: Light level detection ---
LIGHT_LEVEL_INTERVAL_S = float(os.environ.get("HAL_LIGHT_LEVEL_INTERVAL_S", "300.0"))
LIGHT_CHANGE_THRESHOLD = int(os.environ.get("HAL_LIGHT_CHANGE_THRESHOLD", "100"))

# --- Sensing: Face detection ---
USERS_DIR: str = os.environ.get("HAL_USERS_DIR", "/root/local/users")
STRANGERS_DIR: str = os.environ.get("HAL_STRANGERS_DIR", "/root/local/strangers")
YUNET_CONFIDENCE_THRESHOLD = float(
    os.environ.get("HAL_YUNET_CONFIDENCE_THRESHOLD", "0.35")
)
FACE_COOLDOWN_S = float(os.environ.get("HAL_FACE_COOLDOWN_S", "10.0"))
FACE_OWNER_FORGET_S = float(os.environ.get("HAL_FACE_OWNER_FORGET_S", "3600.0"))
FACE_STRANGER_FORGET_S = float(os.environ.get("HAL_FACE_STRANGER_FORGET_S", "1800.0"))
FACE_STRANGER_FLUSH_S = float(os.environ.get("HAL_FACE_STRANGER_FLUSH_S", "10.0"))
FACE_AREA_RATIO_THRESHOLD = float(os.environ.get("HAL_FACE_AREA_RATIO_THRESHOLD", "0.05"))

# --- DL backend connection ---
OS_CONFIG_PATH = os.environ.get("OS_CONFIG_PATH", "/root/config/config.json")

def _os_cfg_get(key: str, default: str = "") -> str:
    """Read a value from the os-server config.json (shared with the Go server)."""
    try:
        import json
        with open(OS_CONFIG_PATH) as f:
            return json.load(f).get(key, default)
    except Exception:
        return default

DL_BACKEND_URL = _os_cfg_get("llm_base_url") or os.environ.get("DL_BACKEND_URL", "")
DL_API_KEY = _os_cfg_get("llm_api_key") or os.environ.get("DL_API_KEY", "")
# Device-internal auth token — the secret a caller presents to reach this HAL,
# kept SEPARATE from the LLM provider key (DL_API_KEY). Falls back to the LLM key
# for backward compatibility with devices provisioned before the split; new
# provisioning should set a distinct device_auth_token. See SECURITY.md.
DEVICE_AUTH_TOKEN = (
    _os_cfg_get("device_auth_token")
    or os.environ.get("HAL_DEVICE_AUTH_TOKEN")
    or DL_API_KEY
)
DL_HEARTBEAT_INTERVAL_S = float(os.environ.get("HAL_DL_HEARTBEAT_INTERVAL_S", "60.0"))
# Max time to wait for a dlbackend WS response (pose/motion frame, heartbeat,
# key exchange). Without this, a non-responding backend blocks the recv() call
# forever, holding a shared perception-pool worker and starving every other
# camera perception (face/light). On timeout the session is dropped + retried.
DL_WS_RECV_TIMEOUT_S = float(os.environ.get("HAL_DL_WS_RECV_TIMEOUT_S", "15.0"))
# Append-only file that records every dlbackend WS stall (recv timeout) so the
# issue can be tracked over time without scraping the journal. One line per
# stall: <iso_ts>\t<task>\t<detail>.
DL_STALL_LOG_FILE = os.environ.get("HAL_DL_STALL_LOG", "/root/local/dl_ws_stall.log")

# --- DL backend encryption (RSA + AES-256-GCM) ---
DL_ENCRYPTION_ENABLED: bool = os.environ.get("HAL_DL_ENCRYPTION", "true").lower() in ("1", "true", "yes")
DL_ENCRYPTION_REQUIRED: bool = os.environ.get("HAL_DL_ENCRYPTION_REQUIRED", "false").lower() in ("1", "true", "yes")
DL_PUBLIC_KEY_FILE: str = os.environ.get("DL_PUBLIC_KEY_FILE", "")
DL_PUBLIC_KEY_ENDPOINT = os.environ.get("DL_PUBLIC_KEY_ENDPOINT", "/crypto/public-key")
DL_PUBLIC_KEY_URL = DL_BACKEND_URL.rstrip("/") + "/" + DL_PUBLIC_KEY_ENDPOINT.strip("/") if DL_BACKEND_URL else ""

# --- DL backend endpoints ---
DL_MOTION_ENDPOINT = os.environ.get("DL_MOTION_ENDPOINT", "/ws/hal/api/dl/action-analysis/ws")
DL_MOTION_BACKEND_URL = DL_BACKEND_URL.rstrip("/") + "/" + DL_MOTION_ENDPOINT.strip("/") if DL_BACKEND_URL else ""
DL_EMOTION_RECOGNIZE_ENDPOINT = os.environ.get("DL_EMOTION_RECOGNIZE_ENDPOINT", "/hal/api/dl/emotion-recognize")
DL_POSE_ENDPOINT = os.environ.get("DL_POSE_ENDPOINT", "/ws/hal/api/dl/pose-estimation/ws")
DL_POSE_BACKEND_URL = DL_BACKEND_URL.rstrip("/") + "/" + DL_POSE_ENDPOINT.strip("/") if DL_BACKEND_URL else ""
DL_SPEAKER_ENDPOINT = os.environ.get("DL_SPEAKER_ENDPOINT", "/hal/api/dl/audio-recognizer/embed")
DL_SPEAKER_BACKEND_URL: str = DL_BACKEND_URL.rstrip("/") + "/" + DL_SPEAKER_ENDPOINT.strip("/") if DL_BACKEND_URL else ""
DL_SER_ENDPOINT: str = os.environ.get("DL_SER_ENDPOINT", "/hal/api/dl/ser/recognize")
DL_SER_BACKEND_URL: str = DL_BACKEND_URL.rstrip("/") + "/" + DL_SER_ENDPOINT.strip("/") if DL_BACKEND_URL else ""

# --- Sensing: Motion detection (action recognition via dlbackend) ---
MOTION_ENABLED = os.environ.get("HAL_MOTION_ENABLED", "true").lower() == "true"
MOTION_PER_FACE_ENABLED = os.environ.get("HAL_MOTION_PER_FACE_ENABLED", "false").lower() == "true"
MOTION_PER_FACE_DEDUP_WINDOW_S = float(os.environ.get("HAL_MOTION_PER_FACE_DEDUP_WINDOW_S", "300.0"))
MOTION_PER_FACE_SESSION_TTL_S = float(os.environ.get("HAL_MOTION_PER_FACE_SESSION_TTL_S", "30.0"))
MOTION_PER_FACE_MIN_FRAMES = int(os.environ.get("HAL_MOTION_PER_FACE_MIN_FRAMES", "4"))
MOTION_CONFIDENCE_THRESHOLD = float(
    os.environ.get("HAL_MOTION_CONFIDENCE_THRESHOLD", "0.3")
)
MOTION_FLUSH_S = float(os.environ.get("HAL_MOTION_FLUSH_S", "10.0"))
MOTION_EVENT_COOLDOWN_S = float(
    os.environ.get("HAL_MOTION_EVENT_COOLDOWN_S", "360.0")
)
MOTION_PERSON_DETECTION_ENABLED = os.environ.get("HAL_MOTION_PERSON_DETECTION_ENABLED", "true").lower() == "true"
MOTION_PERSON_MIN_AREA_RATIO = float(
    os.environ.get("HAL_MOTION_PERSON_MIN_AREA_RATIO", "0.25")
)
MOTION_SNAPSHOT_DIR = os.environ.get(
    "HAL_MOTION_SNAPSHOT_DIR",
    os.path.join(tempfile.gettempdir(), "hal-motion-snapshots"),
)
MOTION_SNAPSHOT_MAX_COUNT = int(os.environ.get("HAL_MOTION_SNAPSHOT_MAX_COUNT", "100"))

# --- Sensing: Emotion detection (face emotion via dlbackend) ---
EMOTION_ENABLED = os.environ.get("HAL_EMOTION_ENABLED", "true").lower() == "true"
EMOTION_CONFIDENCE_THRESHOLD = float(
    os.environ.get("HAL_EMOTION_CONFIDENCE_THRESHOLD", "0.5")
)
EMOTION_FLUSH_S = float(os.environ.get("HAL_EMOTION_FLUSH_S", "10.0"))
EMOTION_DEDUP_WINDOW_S = float(os.environ.get("HAL_EMOTION_DEDUP_WINDOW_S", "300.0"))
EMOTION_SNAPSHOT_DIR = os.environ.get(
    "HAL_EMOTION_SNAPSHOT_DIR",
    os.path.join(tempfile.gettempdir(), "hal-emotion-snapshots"),
)
EMOTION_SNAPSHOT_MAX_COUNT = int(os.environ.get("HAL_EMOTION_SNAPSHOT_MAX_COUNT", "100"))

# --- Sensing: Fire hazard detection (object detection via dlbackend) ---
FIRE_HAZARD_ENABLED = os.environ.get("HAL_FIRE_HAZARD_ENABLED", "true").lower() == "true"
FIRE_HAZARD_CHECK_INTERVAL_S = float(os.environ.get("HAL_FIRE_HAZARD_CHECK_INTERVAL_S", "0"))
FIRE_HAZARD_CONFIDENCE_THRESHOLD = float(os.environ.get("HAL_FIRE_HAZARD_CONFIDENCE_THRESHOLD", "0.3"))
FIRE_HAZARD_OVERLAP_THRESHOLD = float(os.environ.get("HAL_FIRE_HAZARD_OVERLAP_THRESHOLD", "0.2"))
FIRE_HAZARD_CONFIRM_S = float(os.environ.get("HAL_FIRE_HAZARD_CONFIRM_S", "10.0"))
FIRE_HAZARD_DEDUP_WINDOW_S = float(os.environ.get("HAL_FIRE_HAZARD_DEDUP_WINDOW_S", "120.0"))
FIRE_HAZARD_FLUSH_S = float(os.environ.get("HAL_FIRE_HAZARD_FLUSH_S", "10.0"))
FIRE_HAZARD_DETECTOR = os.environ.get("HAL_FIRE_HAZARD_DETECTOR", "owlv2")
FIRE_HAZARD_ENDPOINT = os.environ.get("DL_FIRE_HAZARD_ENDPOINT", f"/detect/{FIRE_HAZARD_DETECTOR}")
FIRE_HAZARD_BACKEND_URL: str = DL_BACKEND_URL.rstrip("/") + "/" + FIRE_HAZARD_ENDPOINT.strip("/") if DL_BACKEND_URL else ""
FIRE_HAZARD_API_TIMEOUT_S = float(os.environ.get("HAL_FIRE_HAZARD_API_TIMEOUT_S", "15.0"))

# --- Sensing: Pose-based motion detection (RTMPose ONNX) ---
POSE_MOTION_ENABLED = (
    os.environ.get("HAL_POSE_MOTION_ENABLED", "true").lower() == "true"
)
POSE_MOTION_MODEL_PATH = Path(os.environ.get("HAL_POSE_MODEL_PATH", "/root/local/models/rtmpose-m.onnx"))
POSE_MOTION_ANGLE_THRESHOLD = float(
    os.environ.get("HAL_POSE_MOTION_ANGLE_THRESHOLD", "30.0")
)

# --- Sensing: Pose estimation + ergonomic assessment (via dlbackend) ---
POSE_ENABLED = os.environ.get("HAL_POSE_ENABLED", "true").lower() == "true"
POSE_ERGO_HIGH_RISK_THRESHOLD = int(os.environ.get("HAL_POSE_ERGO_HIGH_RISK_THRESHOLD", "5"))
# Posture is now sampled silently into a rolling buffer; MotionPerception
# decides when to fold the summary into a motion.activity event.
#
# DEBUG VALUES — sampling 1 / 30s and window 10 min, so a full evaluation
# cycle finishes in ~10 min during live testing (bucket feature shake-down).
# Swap to 60 s / 3600 s for production (one env var each, no code change).
POSE_SAMPLE_INTERVAL_S = float(os.environ.get("HAL_POSE_SAMPLE_INTERVAL_S", "30.0"))
# Tumbling time window. At the end of every WINDOW_DURATION_S, MotionPerception
# evaluates whatever samples have accumulated, decides whether to inject a
# posture nudge, and ALWAYS resets the buffer + window start (regardless of
# fire / no-fire). DEBUG = 600 s (10 min); production target 3600 s (60 min)
# — one variable, no test/prod branches in code.
POSE_WINDOW_DURATION_S = float(os.environ.get("HAL_POSE_WINDOW_DURATION_S", "600.0"))
# Noise floor — if the window completed but had fewer than this many real
# samples (dlbackend missed most frames, presence flicker, etc.), skip the
# inject. Statistical confidence is too low to nag the user.
POSE_WINDOW_MIN_SAMPLES = int(os.environ.get("HAL_POSE_WINDOW_MIN_SAMPLES", "3"))
# Bad-sample definition: any single region (L or R) at sub-score >= this.
# Catches "head thrust forward, rest of body OK" cases that dlbackend's
# whole-body risk_level alone misses (RULA total stays at "low" because
# trunk+arms are fine, but neck sub-score = 4 by itself is worth nagging).
POSE_REGION_HIGH_SUBSCORE = int(os.environ.get("HAL_POSE_REGION_HIGH_SUBSCORE", "4"))
# Fraction of the window that must be "bad" before posture_summary rides
# along on the next motion.activity event. Window-size agnostic.
POSE_BAD_RATIO = float(os.environ.get("HAL_POSE_BAD_RATIO", "0.6"))
# Removed POSE_STREAK_MIN_GATE_S + POSE_NUDGE_COOLDOWN_S — the tumbling
# window is the only timing gate. Window-start is anchored on the first
# sedentary flush, so by the time it completes the user has been at the
# computer for at least POSE_WINDOW_DURATION_S — no separate "streak
# minimum" needed. Window-reset after each cycle means the next fire is
# naturally one window away — no separate cooldown needed.
# Per-sample annotated JPEG retention. Snapshots are grouped per tumbling
# window into buckets/<window_start_int>/<sample_ts_int>_<score>.jpg with
# a bucket.json sidecar. When a window closes:
#   - bad_ratio >= POSE_BAD_RATIO → bucket marked "kept" and survives up
#     to POSE_BUCKET_KEEP_S for monitor replay + /dm image attach.
#   - otherwise → bucket is deleted immediately.
# Kept buckets are pruned oldest-first once the byte cap is exceeded.
POSE_BUCKET_KEEP_S = float(
    os.environ.get("HAL_POSE_BUCKET_KEEP_S", str(2 * 24 * 3600))
)
POSE_SNAPSHOT_MAX_BYTES = int(
    os.environ.get("HAL_POSE_SNAPSHOT_MAX_BYTES", str(50 * 1024 * 1024))
)
# Number of "worst" samples to surface from a kept bucket — used by the
# monitor turn-card preview strip and the Telegram /dm attach. Selection
# combines (highest score, dominant-region rep, latest bad sample).
POSE_WORST_SNAPSHOTS_PER_BUCKET = int(
    os.environ.get("HAL_POSE_WORST_SNAPSHOTS_PER_BUCKET", "3")
)
# TEMPORARY WORKAROUND — dlbackend's signed_flexion_angle returns the
# opposite sign of its docstring ("Positive = forward flexion"): user
# clearly hunched forward produces angle = -72°, not +72°. Flip on
# receive so the monitor table and JSONL match reality. Revert (set to
# False) the moment dlbackend's utils.signed_flexion_angle is fixed
# upstream. Only the three signed angles need flipping; lower_arm_angle
# is unsigned (angle_between_3d) and the RULA scores already use
# abs(angle) so risk_level / score are unaffected.
POSE_FLIP_DLBACKEND_ANGLE_SIGN = (
    os.environ.get("HAL_POSE_FLIP_DLBACKEND_ANGLE_SIGN", "true").lower() == "true"
)

# --- Sensing: Snapshot storage ---
SNAPSHOT_TMP_DIR = os.environ.get(
    "HAL_SNAPSHOT_TMP_DIR", "/tmp/hal-sensing-snapshots"
)
SNAPSHOT_TMP_MAX_COUNT = int(os.environ.get("HAL_SNAPSHOT_TMP_MAX_COUNT", "50"))
SNAPSHOT_PERSIST_DIR = os.environ.get(
    "HAL_SNAPSHOT_PERSIST_DIR", "/var/lib/hal/snapshots"
)
SNAPSHOT_PERSIST_TTL_S = float(
    os.environ.get("HAL_SNAPSHOT_PERSIST_TTL_S", str(72 * 3600))
)
SNAPSHOT_PERSIST_MAX_BYTES = int(
    os.environ.get("HAL_SNAPSHOT_PERSIST_MAX_BYTES", str(50 * 1024 * 1024))
)

# --- Presence: Auto light on/off ---
IDLE_TIMEOUT_S = float(os.environ.get("HAL_IDLE_TIMEOUT_S", "300"))
AWAY_TIMEOUT_S = float(os.environ.get("HAL_AWAY_TIMEOUT_S", "900"))
IDLE_BRIGHTNESS = float(os.environ.get("HAL_IDLE_BRIGHTNESS", "0.20"))

# --- Sensing: Speaker recognition (voice embedding via dlbackend) ---
SPEAKER_RECOGNITION_ENABLED: bool = (
    os.environ.get("HAL_SPEAKER_RECOGNITION_ENABLED", "true").lower() == "true"
)
SPEAKER_MIN_AUDIO_S: float = float(os.environ.get("HAL_SPEAKER_MIN_AUDIO_S", "0.8")) # seconds
SPEAKER_MATCH_THRESHOLD: float = float(os.environ.get("SPEAKER_MATCH_THRESHOLD", "0.7")) # 0.0 - 1.0
SPEAKER_ENROLL_CONSISTENCY_THRESHOLD: float = float(
    os.environ.get("SPEAKER_ENROLL_CONSISTENCY_THRESHOLD", "0.7")
)
SPEAKER_EMBEDDING_API_TIMEOUT_S: float = float(
    os.environ.get("SPEAKER_EMBEDDING_API_TIMEOUT_S", "15")
)
SPEAKER_UNKNOWN_AUDIO_DIR: str = os.environ.get(
    "HAL_UNKNOWN_AUDIO_DIR",
    os.path.join(tempfile.gettempdir(), "hal-unknown-voice"),
)
DL_SPEAKER_ENDPOINT = os.environ.get("DL_SPEAKER_ENDPOINT", "/hal/api/dl/audio-recognizer/embed")
SPEAKER_EMBEDDING_API_URL: str = DL_BACKEND_URL.rstrip("/") + "/" + DL_SPEAKER_ENDPOINT.strip("/") if DL_BACKEND_URL else ""
SPEAKER_EMBEDDING_API_KEY: str = DL_API_KEY

# --- Sensing: Speech emotion recognition (SER via dlbackend) ---
SPEECH_EMOTION_ENABLED: bool = (
    os.environ.get("HAL_SPEECH_EMOTION_ENABLED", "true").lower() == "true"
)
SPEECH_EMOTION_FLUSH_S: float = float(
    os.environ.get("HAL_SPEECH_EMOTION_FLUSH_S", "10.0")
)
SPEECH_EMOTION_DEDUP_WINDOW_S: float = float(
    os.environ.get("HAL_SPEECH_EMOTION_DEDUP_WINDOW_S", "300.0")
)
SPEECH_EMOTION_MIN_AUDIO_S: float = float(
    os.environ.get("HAL_SPEECH_EMOTION_MIN_AUDIO_S", "3.0")
)
SPEECH_EMOTION_API_TIMEOUT_S: float = float(
    os.environ.get("HAL_SPEECH_EMOTION_API_TIMEOUT_S", "15")
)
DL_SER_ENDPOINT: str = os.environ.get(
    "DL_SER_ENDPOINT", "/hal/api/dl/ser/recognize"
)
SPEECH_EMOTION_API_URL: str = (
    DL_BACKEND_URL.rstrip("/") + "/" + DL_SER_ENDPOINT.strip("/")
    if DL_BACKEND_URL else ""
)
SPEECH_EMOTION_API_KEY: str = DL_API_KEY
SPEECH_EMOTION_AUDIO_DIR: str = os.environ.get(
    "HAL_SPEECH_EMOTION_AUDIO_DIR",
    os.path.join(tempfile.gettempdir(), "hal-speech-emotion"),
)

# --- Agent gateway ---
# Mirrors the Go server's agent/factory.go cascade: env > config.json > default.
AGENT_GATEWAY: str = (
    os.environ.get("HAL_AGENT_GATEWAY")
    or _os_cfg_get("agent_runtime")
    or "openclaw"
).strip().lower()

# --- Realtime voice agent ---
# Operator overrides for the realtime voice agent come from the nested "realtime"
# block in os-server's config.json (written by the web UI; modelled in Go at
# server/config/realtime.go). HAL reads it DIRECTLY here — same pattern as
# llm_api_key / stt_language via _os_cfg_get — rather than having os-server push
# it down through the agent gateway. Precedence per knob: HAL_* env var (dev
# override) > realtime block > built-in default. NOTE: read once at import, so a
# config change needs a HAL restart to take effect.
def _os_cfg_realtime() -> dict:
    """The nested 'realtime' dict from os-server config.json, or {} if absent."""
    try:
        import json
        with open(OS_CONFIG_PATH) as f:
            rt = json.load(f).get("realtime")
        return rt if isinstance(rt, dict) else {}
    except Exception:
        return {}


_RT: dict = _os_cfg_realtime()
_RT_GEMINI: dict = _RT.get("gemini") if isinstance(_RT.get("gemini"), dict) else {}
_RT_OPENAI: dict = _RT.get("openai") if isinstance(_RT.get("openai"), dict) else {}


def _rt_str(env_key: str, cfg_val, default: str) -> str:
    """Resolve a realtime string knob: env var > config.json value > default."""
    env = os.environ.get(env_key)
    if env:
        return env
    if cfg_val:
        return str(cfg_val)
    return default


def _rt_enabled() -> bool:
    env = os.environ.get("HAL_REALTIME_ENABLED")
    if env is not None:
        return env.lower() in ("1", "true", "yes")
    if "enabled" in _RT:
        return bool(_RT["enabled"])
    return True


REALTIME_ENABLED: bool = _rt_enabled()
REALTIME_PROVIDER: str = _rt_str("HAL_REALTIME_PROVIDER", _RT.get("provider"), "gemini")  # none | gemini | openai
# Max seconds receive() waits for the NEXT output event from the agent's recv
# queue before giving up on the turn. This is the gap between events, not the
# whole turn: a streaming reply puts events on the queue sub-second apart and
# ends with a turn-done signal, so this only fires when the model stays SILENT
# (a noise/non-directed turn it correctly ignores, or a stall). It is therefore
# the dead-air the user waits through before the turn falls back to the main
# agent — keep it just above realtime first-token latency (~1-2s), not minutes.
REALTIME_RECV_QUEUE_TIMEOUT_S: float = float(
    os.environ.get("HAL_REALTIME_RECV_QUEUE_TIMEOUT_S", "8.0")
)
# Zombie-session guard. A long-lived Gemini Live session can stop responding
# (the campaign-api proxy doesn't always relay Gemini's go_away/close, so the
# WS stays "connected", accepts audio, but never replies — every turn hits the
# recv timeout above). The normal reconnect only fires on an explicit WS
# error/close, which never arrives here, so the session stays zombie until a
# HAL restart. After this many CONSECUTIVE silent turns (committed audio, zero
# output) we force a fresh session — what a manual restart does, automatically.
# Consecutive (not total) so genuine interspersed noise turns don't trip it.
REALTIME_ZOMBIE_RECONNECT_AFTER: int = int(
    os.environ.get("HAL_REALTIME_ZOMBIE_RECONNECT_AFTER", "3")
)
# Cost control: recycle (rebuild) the realtime session when a new turn arrives
# after this many seconds of silence. A long-lived session accumulates per-turn
# context the provider (Gemini Live / OpenAI Realtime) re-bills every turn; a turn
# that follows a long pause is effectively a new conversation, so starting a fresh
# session then drops that accumulation. Long-term continuity survives — the rebuild
# reloads the persisted summary.md. 0 disables. Default 240s (4 min). See
# RealtimeOrchestrator._mark_turn_start.
REALTIME_SESSION_IDLE_RESET_S: float = float(
    os.environ.get("HAL_REALTIME_SESSION_IDLE_RESET_S", "240")
)
# Cost control: recycle (rebuild) the realtime session after this many turns even
# in an actively-ongoing conversation. Each turn's reply + audio accrues into the
# session context the provider re-bills as input every turn, so context grows
# unbounded in a long chat; recycling caps that growth back to the floor
# (instructions + summary). Continuity survives via the reloaded summary.md. 0
# disables. See RealtimeOrchestrator.stream_output.
REALTIME_SESSION_MAX_TURNS: int = int(
    os.environ.get("HAL_REALTIME_SESSION_MAX_TURNS", "12")
)
# A captured session shorter than this AND with no STT transcript is treated as a
# VAD false-trigger (a noise blip that only grabbed the pre-roll, no sustained
# speech) and is NOT committed to the realtime model. Committing such turns wastes
# a model turn and often makes it answer the silence, which then desyncs onto a
# later real turn. A genuine audio-only turn (real speech STT happened to miss)
# runs longer than this, so it still commits.
REALTIME_MIN_COMMIT_DURATION_S: float = float(
    os.environ.get("HAL_REALTIME_MIN_COMMIT_DURATION_S", "0.8")
)
# Noise guard for empty-STT turns: the duration floor above only catches SHORT
# noise blips — sustained background noise (fan, hum) runs longer than the floor,
# fools the entry VAD, yields no STT transcript, yet still commits to the realtime
# model, which then answers the noise (spurious self-talk + wasted tokens). When
# enabled, an empty-STT turn is re-checked with Silero VAD over the FULL captured
# buffer; if it isn't speech, the turn is dropped regardless of duration. A genuine
# audio-only turn (real speech STT missed) passes Silero, so it still commits.
# Fail-open: Silero unavailable/erroring → behaves as before (commits).
REALTIME_REQUIRE_SPEECH_ON_EMPTY_STT: bool = os.environ.get(
    "HAL_REALTIME_REQUIRE_SPEECH_ON_EMPTY_STT", "true"
).lower() in ("1", "true", "yes")
# Voiced-ratio floor for the empty-STT noise guard: the fraction of 32ms Silero
# chunks that must be voiced for the buffer to count as real speech (and commit).
# Peak confidence alone is too lenient — one transient chunk crossing the Silero
# threshold would pass a noisy turn — so we require sustained voicing. A real
# speaking turn is voiced across most of its length; sustained noise spikes only
# sparsely. Provisional 0.30; tune from the `noise-guard metrics` logs.
# Empty-STT turns are committed to Gemini (full history + audio re-billed) only if
# their voiced ratio clears this bar — the main guard against noise/false-trigger
# turns inflating cost ("387 requests" when far fewer were real). Device data shows
# a clean gap: real speech sits >=0.64 voiced, noise that leaked sat 0.30-0.55. Set
# at 0.55 to drop the noise band while keeping real speech. Raise if noise still
# leaks; lower if real short/quiet utterances get dropped.
REALTIME_NOISE_SPEECH_RATIO: float = float(
    os.environ.get("HAL_REALTIME_NOISE_SPEECH_RATIO", "0.55")
)
# Turn detection / VAD: "server_vad" | "semantic_vad" | "off"
# For Gemini: "off" disables automatic activity detection; any other value enables it.
# For OpenAI: maps to turn_detection type in session config.
REALTIME_TURN_DETECTION: str = os.environ.get("HAL_REALTIME_TURN_DETECTION", "off")

# Native voice: for chit-chat handled by the realtime model, play the model's OWN
# audio output (Gemini Live / OpenAI Realtime voice) straight to the speaker
# instead of re-synthesizing the transcript through our ElevenLabs TTS. Lower
# latency + native prosody, but loses the configured ElevenLabs voice. Default
# off → keep the ElevenLabs path. Delegated turns are unaffected (spoken by the
# main agent via TTS regardless). env > config.json `realtime.native_audio` > default.
REALTIME_NATIVE_AUDIO: bool = os.environ.get(
    "HAL_REALTIME_NATIVE_AUDIO", str(_RT.get("native_audio", False))
).lower() in ("1", "true", "yes")

# --- Realtime: Gemini Live ---
REALTIME_GEMINI_API_KEY: str = (
    os.environ.get("GEMINI_API_KEY", "")
    or os.environ.get("GOOGLE_API_KEY", "")
    or _RT.get("api_key", "")
    or _os_cfg_get("llm_api_key", "")
)
REALTIME_GEMINI_BASE_URL: str = (
    os.environ.get("HAL_GEMINI_LIVE_BASE_URL", "")
    or _RT.get("base_url", "")
    or ((_os_cfg_get("llm_base_url", "").rstrip("/") + "/ws/gemini") if _os_cfg_get("llm_base_url", "") else "")
)
REALTIME_GEMINI_MODEL: str = _rt_str("HAL_GEMINI_LIVE_MODEL", _RT_GEMINI.get("model"), "gemini-3.1-flash-live-preview")
REALTIME_GEMINI_VOICE: str = _rt_str("HAL_GEMINI_LIVE_VOICE", _RT_GEMINI.get("voice"), "Kore")
REALTIME_GEMINI_SAMPLE_RATE: int = 16000
REALTIME_GEMINI_THINKING_LEVEL: str = _rt_str("HAL_GEMINI_THINKING_LEVEL", _RT_GEMINI.get("thinking_level"), "MINIMAL")
REALTIME_GEMINI_USE_LANGUAGE_CODES: bool = os.environ.get("HAL_GEMINI_USE_LANGUAGE_CODES", "false").lower() in ("1", "true", "yes")
# Context-window compression (COST): Gemini re-bills the whole accumulated session
# (system instruction + conversation history) as input text on EVERY turn, so a long
# session's growing history dominates input-text cost. The default SlidingWindow()
# never actually triggers in practice (sessions recycle long before its ~100k-token
# default), so we set an explicit LOW trigger: when context exceeds trigger_tokens,
# Gemini compresses the history down to ~target_tokens. This caps per-turn in_text
# around target..trigger instead of letting it climb toward 20k+. Lower = cheaper but
# less recent conversation kept verbatim. trigger MUST be > target.
REALTIME_GEMINI_COMPRESSION_TRIGGER_TOKENS: int = int(
    os.environ.get("HAL_GEMINI_COMPRESSION_TRIGGER_TOKENS", "14000")
)
REALTIME_GEMINI_COMPRESSION_TARGET_TOKENS: int = int(
    os.environ.get("HAL_GEMINI_COMPRESSION_TARGET_TOKENS", "7000")
)
# Keepalive (RELIABILITY): the campaign-api proxy idle-closes the Gemini Live WS
# after a short silence (~40s). When the next user turn lands on the just-closed
# session, the committed audio is lost on cold-reconnect and the model stays silent
# → the user's question is dropped. Send a WS ping every N seconds while idle to
# keep the session warm. 0 = off. Must be < the proxy idle window.
REALTIME_GEMINI_KEEPALIVE_S: float = float(
    os.environ.get("HAL_GEMINI_KEEPALIVE_S", "8")
)
# Session resumption lets a reconnect resume the SAME server session (context
# preserved). It requires the WS endpoint to faithfully forward the resumption
# handshake — the autonomous `campaign-api` proxy does NOT, so resuming through it
# yields a zombie session: connected and accepting audio but never producing
# output. Cold reconnects (a fresh session each time) work through the proxy, so
# this defaults OFF. Enable only against an endpoint that supports resumption
# (e.g. a direct Google base_url).
REALTIME_GEMINI_SESSION_RESUMPTION: bool = os.environ.get(
    "HAL_GEMINI_SESSION_RESUMPTION", "false"
).lower() in ("1", "true", "yes")
# Google Search grounding lets Gemini Live answer live-data questions (weather,
# news, lookups) directly in the realtime session instead of delegating to main —
# faster, and it skips a full main-agent turn. It bills per grounded request on
# top of tokens, but only fires when Gemini actually decides to search (the prompt
# tells it to ground only for genuine live data, not general knowledge). Defaults
# ON; env HAL_GEMINI_GOOGLE_SEARCH or realtime.gemini.google_search overrides.
REALTIME_GEMINI_GOOGLE_SEARCH: bool = (
    os.environ.get(
        "HAL_GEMINI_GOOGLE_SEARCH",
        str(_RT_GEMINI.get("google_search", True)),
    ).lower()
    in ("1", "true", "yes")
)

# --- Realtime: OpenAI Realtime ---
REALTIME_OPENAI_API_KEY: str = (
    os.environ.get("OPENAI_API_KEY", "")
    or _RT.get("api_key", "")
    or _os_cfg_get("llm_api_key", "")
)
REALTIME_OPENAI_BASE_URL: str = (
    os.environ.get("HAL_OPENAI_REALTIME_BASE_URL", "")
    or _RT.get("base_url", "")
    or ((_os_cfg_get("llm_base_url", "").rstrip("/") + "/ws/openai") if _os_cfg_get("llm_base_url", "") else "")
)
REALTIME_OPENAI_MODEL: str = _rt_str("HAL_OPENAI_REALTIME_MODEL", _RT_OPENAI.get("model"), "gpt-realtime-2")
REALTIME_OPENAI_VOICE: str = _rt_str("HAL_OPENAI_REALTIME_VOICE", _RT_OPENAI.get("voice"), "alloy")
REALTIME_OPENAI_SAMPLE_RATE: int = 24000
REALTIME_OPENAI_REASONING_EFFORT: str = _rt_str("HAL_OPENAI_REASONING_EFFORT", _RT_OPENAI.get("reasoning_effort"), "minimal")

# --- Realtime: Context manager ---
OPENCLAW_WORKSPACE_DIR: str = os.environ.get("HAL_OPENCLAW_WORKSPACE_DIR", "/root/.openclaw/workspace")
HERMES_WORKSPACE_DIR: str = os.environ.get("HAL_HERMES_WORKSPACE_DIR", "/root/.hermes")
_rt_workspace: str = OPENCLAW_WORKSPACE_DIR.rstrip("/")
REALTIME_MEMORY_PATH: str = os.environ.get("HAL_REALTIME_MEMORY_PATH", f"{_rt_workspace}/realtime/memory.jsonl")
REALTIME_MAX_MEMORY_ENTRIES: int = int(os.environ.get("HAL_REALTIME_MAX_MEMORY_ENTRIES", "1000"))
REALTIME_MEMORY_TRIM_KEEP: int = int(os.environ.get("HAL_REALTIME_MEMORY_TRIM_KEEP", "500"))
# These bound the DEVICE MEMORY / REALTIME MEMORY sections of the per-turn floor
# (build_instructions), which Gemini re-bills every turn. 100k chars (~25k tokens)
# each was far too generous for something billed per-turn — capped to ~16k chars
# (~4k tokens). Also makes realtime-memory summarization trigger sooner (fresher
# in-context memory). The full history is preserved in summary.md + memory_raw.jsonl.
REALTIME_DEVICE_MEMORY_MAX_CHARS: int = int(os.environ.get("HAL_REALTIME_DEVICE_MEMORY_MAX_CHARS", "16000"))
REALTIME_MEMORY_MAX_CHARS: int = int(os.environ.get("HAL_REALTIME_MEMORY_MAX_CHARS", "16000"))
# Cap on the rolling realtime summary.md — part of the per-turn floor, so kept
# tight (~1.5k tokens). Env-overridable for tuning.
REALTIME_SUMMARY_MAX_CHARS: int = int(os.environ.get("HAL_REALTIME_SUMMARY_MAX_CHARS", "5000"))

# --- Realtime: Summarizer (Anthropic Messages API) ---
REALTIME_SUMMARIZER_ENABLED: bool = os.environ.get("HAL_REALTIME_SUMMARIZER_ENABLED", "true").lower() in ("1", "true", "yes")
REALTIME_SUMMARIZER_API_KEY: str = os.environ.get("HAL_REALTIME_SUMMARIZER_API_KEY", "") or _os_cfg_get("llm_api_key", "")
# Anthropic SDK appends /v1/messages, so strip trailing /v1 from llm_base_url
_summarizer_base: str = os.environ.get("HAL_REALTIME_SUMMARIZER_BASE_URL", "") or _os_cfg_get("llm_base_url", "")
REALTIME_SUMMARIZER_BASE_URL: str = _summarizer_base.rstrip("/").removesuffix("/v1") if _summarizer_base else ""
REALTIME_SUMMARIZER_MODEL: str = os.environ.get("HAL_REALTIME_SUMMARIZER_MODEL", "claude-haiku-4-5-20251001")
