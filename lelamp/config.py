"""
LeLamp runtime configuration — all values read from environment variables.

Import: from lelamp.config import LAMP_ID, SERVO_PORT, ...
"""

import os
import tempfile
from pathlib import Path
from typing import Optional, Union

# --- Hardware ---
SERVO_PORT = os.environ.get("LELAMP_SERVO_PORT", "/dev/ttyACM0")
LAMP_ID = os.environ.get("LELAMP_LAMP_ID", "lelamp")
SERVO_FPS = int(os.environ.get("LELAMP_SERVO_FPS", "30"))
SERVO_HOLD_S = float(os.environ.get("LELAMP_SERVO_HOLD_S", "3.0"))
HTTP_PORT = int(os.environ.get("LELAMP_HTTP_PORT", "5001"))
# production (default): bind 127.0.0.1, local-only middleware enforced.
# developer: bind 0.0.0.0, no access restrictions — for local dev/testing only.
_mode = os.environ.get("LELAMP_MODE", "production").strip().lower()
MODE: str = "developer" if _mode == "developer" else "production"
HTTP_HOST: str = "0.0.0.0" if MODE == "developer" else "127.0.0.1"
CAMERA_INDEX = int(os.environ.get("LELAMP_CAMERA_INDEX", "0"))
CAMERA_WIDTH = int(os.environ.get("LELAMP_CAMERA_WIDTH", "640"))
CAMERA_HEIGHT = int(os.environ.get("LELAMP_CAMERA_HEIGHT", "480"))

# --- Audio ---
# Hardware overrides — set in .env to bypass auto-detection
# e.g. LELAMP_AUDIO_INPUT_ALSA=plughw:1,0  LELAMP_AUDIO_OUTPUT_ALSA=plughw:2,0
AUDIO_INPUT_ALSA: Optional[str] = os.environ.get("LELAMP_AUDIO_INPUT_ALSA") or None
AUDIO_OUTPUT_ALSA: Optional[str] = os.environ.get("LELAMP_AUDIO_OUTPUT_ALSA") or None
# Separate mic device for SoundPerception (noise sensing).
# Accepts int (sounddevice index) or string (ALSA device name like "plughw:6,0").
_sensing_device_env = os.environ.get("LELAMP_AUDIO_SENSING_DEVICE")
AUDIO_SENSING_DEVICE: Optional[Union[int, str]] = None
if _sensing_device_env:
    try:
        AUDIO_SENSING_DEVICE = int(_sensing_device_env)
    except ValueError:
        AUDIO_SENSING_DEVICE = _sensing_device_env
# TTS speed multiplier — 1.0=normal, 1.3=faster, max 4.0
TTS_SPEED: float = float(os.environ.get("LELAMP_TTS_SPEED", "1.3"))
# TTS voice — one of: alloy, ash, coral, echo, fable, onyx, nova, sage, shimmer
TTS_VOICE: str = os.environ.get("TTS_VOICE", "nova")
# TTS instructions — style/vibe prompt for voice (e.g. "Speak warmly like a caring friend")
TTS_INSTRUCTIONS: str = os.environ.get("LELAMP_TTS_INSTRUCTIONS", "Friendly")

# --- Vision tracking ---
# Use the local YOLOv8n model for COCO-class targets (person, cup, etc.).
# Set LELAMP_TRACKING_DETECT_LOCAL=false to force remote YOLOWorld for everything
# (slower, but open vocabulary and lighter on the Pi CPU).
TRACKING_DETECT_LOCAL_ENABLED: bool = os.environ.get(
    "LELAMP_TRACKING_DETECT_LOCAL", "true"
).strip().lower() in ("1", "true", "yes", "on")

# Use the local YuNet face detector for target='face' (COCO has no face class,
# YOLO falls back to remote YOLOWorld ~1.3s otherwise). Disable to force remote.
TRACKING_FACE_DETECTOR_ENABLED: bool = os.environ.get(
    "LELAMP_TRACKING_FACE_DETECTOR", "true"
).strip().lower() in ("1", "true", "yes", "on")

# --- Data layout ---

# --- Sensing: Lamp integration ---
LAMP_SENSING_URL = "http://127.0.0.1:5000/api/sensing/event"
LAMP_WELLBEING_LOG_URL = "http://127.0.0.1:5000/api/wellbeing/log"

# --- Sensing: Event cooldown ---
EVENT_COOLDOWN_S = float(os.environ.get("LELAMP_EVENT_COOLDOWN_S", "60.0"))

# --- Sensing: Sound detection ---
SOUND_RMS_THRESHOLD = int(os.environ.get("LELAMP_SOUND_RMS_THRESHOLD", "8000"))
SOUND_SAMPLE_DURATION_S = float(os.environ.get("LELAMP_SOUND_SAMPLE_DURATION_S", "0.5"))

# --- Sensing: Light level detection ---
LIGHT_LEVEL_INTERVAL_S = float(os.environ.get("LELAMP_LIGHT_LEVEL_INTERVAL_S", "300.0"))
LIGHT_CHANGE_THRESHOLD = int(os.environ.get("LELAMP_LIGHT_CHANGE_THRESHOLD", "100"))

# --- Sensing: Face detection ---
USERS_DIR: str = os.environ.get("LELAMP_USERS_DIR", "/root/local/users")
STRANGERS_DIR: str = os.environ.get("LELAMP_STRANGERS_DIR", "/root/local/strangers")
YUNET_CONFIDENCE_THRESHOLD = float(
    os.environ.get("LELAMP_YUNET_CONFIDENCE_THRESHOLD", "0.35")
)
FACE_COOLDOWN_S = float(os.environ.get("LELAMP_FACE_COOLDOWN_S", "10.0"))
FACE_OWNER_FORGET_S = float(os.environ.get("LELAMP_FACE_OWNER_FORGET_S", "3600.0"))
FACE_STRANGER_FORGET_S = float(os.environ.get("LELAMP_FACE_STRANGER_FORGET_S", "1800.0"))
FACE_STRANGER_FLUSH_S = float(os.environ.get("LELAMP_FACE_STRANGER_FLUSH_S", "10.0"))
FACE_AREA_RATIO_THRESHOLD = float(os.environ.get("LELAMP_FACE_AREA_RATIO_THRESHOLD", "0.05"))

# --- DL backend connection ---
LAMP_CONFIG_PATH = os.environ.get("LAMP_CONFIG_PATH", "/root/config/config.json")

def _lamp_cfg_get(key: str, default: str = "") -> str:
    """Read a value from Lamp's config.json (shared with Go server)."""
    try:
        import json
        with open(LAMP_CONFIG_PATH) as f:
            return json.load(f).get(key, default)
    except Exception:
        return default

DL_BACKEND_URL = _lamp_cfg_get("llm_base_url") or os.environ.get("DL_BACKEND_URL", "")
DL_API_KEY = _lamp_cfg_get("llm_api_key") or os.environ.get("DL_API_KEY", "")
DL_HEARTBEAT_INTERVAL_S = float(os.environ.get("LELAMP_DL_HEARTBEAT_INTERVAL_S", "60.0"))
# Max time to wait for a dlbackend WS response (pose/motion frame, heartbeat,
# key exchange). Without this, a non-responding backend blocks the recv() call
# forever, holding a shared perception-pool worker and starving every other
# camera perception (face/light). On timeout the session is dropped + retried.
DL_WS_RECV_TIMEOUT_S = float(os.environ.get("LELAMP_DL_WS_RECV_TIMEOUT_S", "15.0"))
# Append-only file that records every dlbackend WS stall (recv timeout) so the
# issue can be tracked over time without scraping the journal. One line per
# stall: <iso_ts>\t<task>\t<detail>.
DL_STALL_LOG_FILE = os.environ.get("LELAMP_DL_STALL_LOG", "/root/local/dl_ws_stall.log")

# --- DL backend encryption (RSA + AES-256-GCM) ---
DL_ENCRYPTION_ENABLED: bool = os.environ.get("LELAMP_DL_ENCRYPTION", "true").lower() in ("1", "true", "yes")
DL_ENCRYPTION_REQUIRED: bool = os.environ.get("LELAMP_DL_ENCRYPTION_REQUIRED", "false").lower() in ("1", "true", "yes")
DL_PUBLIC_KEY_FILE: str = os.environ.get("DL_PUBLIC_KEY_FILE", "")
DL_PUBLIC_KEY_ENDPOINT = os.environ.get("DL_PUBLIC_KEY_ENDPOINT", "/crypto/public-key")
DL_PUBLIC_KEY_URL = DL_BACKEND_URL.rstrip("/") + "/" + DL_PUBLIC_KEY_ENDPOINT.strip("/") if DL_BACKEND_URL else ""

# --- DL backend endpoints ---
DL_MOTION_ENDPOINT = os.environ.get("DL_MOTION_ENDPOINT", "/ws/lelamp/api/dl/action-analysis/ws")
DL_MOTION_BACKEND_URL = DL_BACKEND_URL.rstrip("/") + "/" + DL_MOTION_ENDPOINT.strip("/") if DL_BACKEND_URL else ""
DL_EMOTION_RECOGNIZE_ENDPOINT = os.environ.get("DL_EMOTION_RECOGNIZE_ENDPOINT", "/lelamp/api/dl/emotion-recognize")
DL_POSE_ENDPOINT = os.environ.get("DL_POSE_ENDPOINT", "/ws/lelamp/api/dl/pose-estimation/ws")
DL_POSE_BACKEND_URL = DL_BACKEND_URL.rstrip("/") + "/" + DL_POSE_ENDPOINT.strip("/") if DL_BACKEND_URL else ""
DL_SPEAKER_ENDPOINT = os.environ.get("DL_SPEAKER_ENDPOINT", "/lelamp/api/dl/audio-recognizer/embed")
DL_SPEAKER_BACKEND_URL: str = DL_BACKEND_URL.rstrip("/") + "/" + DL_SPEAKER_ENDPOINT.strip("/") if DL_BACKEND_URL else ""
DL_SER_ENDPOINT: str = os.environ.get("DL_SER_ENDPOINT", "/lelamp/api/dl/ser/recognize")
DL_SER_BACKEND_URL: str = DL_BACKEND_URL.rstrip("/") + "/" + DL_SER_ENDPOINT.strip("/") if DL_BACKEND_URL else ""

# --- Sensing: Motion detection (action recognition via dlbackend) ---
MOTION_ENABLED = os.environ.get("LELAMP_MOTION_ENABLED", "true").lower() == "true"
MOTION_PER_FACE_ENABLED = os.environ.get("LELAMP_MOTION_PER_FACE_ENABLED", "false").lower() == "true"
MOTION_PER_FACE_DEDUP_WINDOW_S = float(os.environ.get("LELAMP_MOTION_PER_FACE_DEDUP_WINDOW_S", "300.0"))
MOTION_PER_FACE_SESSION_TTL_S = float(os.environ.get("LELAMP_MOTION_PER_FACE_SESSION_TTL_S", "30.0"))
MOTION_PER_FACE_MIN_FRAMES = int(os.environ.get("LELAMP_MOTION_PER_FACE_MIN_FRAMES", "4"))
MOTION_CONFIDENCE_THRESHOLD = float(
    os.environ.get("LELAMP_MOTION_CONFIDENCE_THRESHOLD", "0.3")
)
MOTION_FLUSH_S = float(os.environ.get("LELAMP_MOTION_FLUSH_S", "10.0"))
MOTION_EVENT_COOLDOWN_S = float(
    os.environ.get("LELAMP_MOTION_EVENT_COOLDOWN_S", "360.0")
)
MOTION_PERSON_DETECTION_ENABLED = os.environ.get("LELAMP_MOTION_PERSON_DETECTION_ENABLED", "true").lower() == "true"
MOTION_PERSON_MIN_AREA_RATIO = float(
    os.environ.get("LELAMP_MOTION_PERSON_MIN_AREA_RATIO", "0.25")
)
MOTION_SNAPSHOT_DIR = os.environ.get(
    "LELAMP_MOTION_SNAPSHOT_DIR",
    os.path.join(tempfile.gettempdir(), "lamp-motion-snapshots"),
)
MOTION_SNAPSHOT_MAX_COUNT = int(os.environ.get("LELAMP_MOTION_SNAPSHOT_MAX_COUNT", "100"))

# --- Sensing: Emotion detection (face emotion via dlbackend) ---
EMOTION_ENABLED = os.environ.get("LELAMP_EMOTION_ENABLED", "true").lower() == "true"
EMOTION_CONFIDENCE_THRESHOLD = float(
    os.environ.get("LELAMP_EMOTION_CONFIDENCE_THRESHOLD", "0.5")
)
EMOTION_FLUSH_S = float(os.environ.get("LELAMP_EMOTION_FLUSH_S", "10.0"))
EMOTION_DEDUP_WINDOW_S = float(os.environ.get("LELAMP_EMOTION_DEDUP_WINDOW_S", "300.0"))
EMOTION_SNAPSHOT_DIR = os.environ.get(
    "LELAMP_EMOTION_SNAPSHOT_DIR",
    os.path.join(tempfile.gettempdir(), "lamp-emotion-snapshots"),
)
EMOTION_SNAPSHOT_MAX_COUNT = int(os.environ.get("LELAMP_EMOTION_SNAPSHOT_MAX_COUNT", "100"))

# --- Sensing: Pose-based motion detection (RTMPose ONNX) ---
POSE_MOTION_ENABLED = (
    os.environ.get("LELAMP_POSE_MOTION_ENABLED", "true").lower() == "true"
)
POSE_MOTION_MODEL_PATH = Path(os.environ.get("LELAMP_POSE_MODEL_PATH", "/root/local/models/rtmpose-m.onnx"))
POSE_MOTION_ANGLE_THRESHOLD = float(
    os.environ.get("LELAMP_POSE_MOTION_ANGLE_THRESHOLD", "30.0")
)

# --- Sensing: Pose estimation + ergonomic assessment (via dlbackend) ---
POSE_ENABLED = os.environ.get("LELAMP_POSE_ENABLED", "true").lower() == "true"
POSE_ERGO_HIGH_RISK_THRESHOLD = int(os.environ.get("LELAMP_POSE_ERGO_HIGH_RISK_THRESHOLD", "5"))
# Posture is now sampled silently into a rolling buffer; MotionPerception
# decides when to fold the summary into a motion.activity event.
#
# DEBUG VALUES — sampling 1 / 30s and window 10 min, so a full evaluation
# cycle finishes in ~10 min during live testing (bucket feature shake-down).
# Swap to 60 s / 3600 s for production (one env var each, no code change).
POSE_SAMPLE_INTERVAL_S = float(os.environ.get("LELAMP_POSE_SAMPLE_INTERVAL_S", "30.0"))
# Tumbling time window. At the end of every WINDOW_DURATION_S, MotionPerception
# evaluates whatever samples have accumulated, decides whether to inject a
# posture nudge, and ALWAYS resets the buffer + window start (regardless of
# fire / no-fire). DEBUG = 600 s (10 min); production target 3600 s (60 min)
# — one variable, no test/prod branches in code.
POSE_WINDOW_DURATION_S = float(os.environ.get("LELAMP_POSE_WINDOW_DURATION_S", "600.0"))
# Noise floor — if the window completed but had fewer than this many real
# samples (dlbackend missed most frames, presence flicker, etc.), skip the
# inject. Statistical confidence is too low to nag the user.
POSE_WINDOW_MIN_SAMPLES = int(os.environ.get("LELAMP_POSE_WINDOW_MIN_SAMPLES", "3"))
# Bad-sample definition: any single region (L or R) at sub-score >= this.
# Catches "head thrust forward, rest of body OK" cases that dlbackend's
# whole-body risk_level alone misses (RULA total stays at "low" because
# trunk+arms are fine, but neck sub-score = 4 by itself is worth nagging).
POSE_REGION_HIGH_SUBSCORE = int(os.environ.get("LELAMP_POSE_REGION_HIGH_SUBSCORE", "4"))
# Fraction of the window that must be "bad" before posture_summary rides
# along on the next motion.activity event. Window-size agnostic.
POSE_BAD_RATIO = float(os.environ.get("LELAMP_POSE_BAD_RATIO", "0.6"))
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
    os.environ.get("LELAMP_POSE_BUCKET_KEEP_S", str(2 * 24 * 3600))
)
POSE_SNAPSHOT_MAX_BYTES = int(
    os.environ.get("LELAMP_POSE_SNAPSHOT_MAX_BYTES", str(50 * 1024 * 1024))
)
# Number of "worst" samples to surface from a kept bucket — used by the
# monitor turn-card preview strip and the Telegram /dm attach. Selection
# combines (highest score, dominant-region rep, latest bad sample).
POSE_WORST_SNAPSHOTS_PER_BUCKET = int(
    os.environ.get("LELAMP_POSE_WORST_SNAPSHOTS_PER_BUCKET", "3")
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
    os.environ.get("LELAMP_POSE_FLIP_DLBACKEND_ANGLE_SIGN", "true").lower() == "true"
)

# --- Sensing: Snapshot storage ---
SNAPSHOT_TMP_DIR = os.environ.get(
    "LELAMP_SNAPSHOT_TMP_DIR", "/tmp/lamp-sensing-snapshots"
)
SNAPSHOT_TMP_MAX_COUNT = int(os.environ.get("LELAMP_SNAPSHOT_TMP_MAX_COUNT", "50"))
SNAPSHOT_PERSIST_DIR = os.environ.get(
    "LELAMP_SNAPSHOT_PERSIST_DIR", "/var/lib/lelamp/snapshots"
)
SNAPSHOT_PERSIST_TTL_S = float(
    os.environ.get("LELAMP_SNAPSHOT_PERSIST_TTL_S", str(72 * 3600))
)
SNAPSHOT_PERSIST_MAX_BYTES = int(
    os.environ.get("LELAMP_SNAPSHOT_PERSIST_MAX_BYTES", str(50 * 1024 * 1024))
)

# --- Presence: Auto light on/off ---
IDLE_TIMEOUT_S = float(os.environ.get("LELAMP_IDLE_TIMEOUT_S", "300"))
AWAY_TIMEOUT_S = float(os.environ.get("LELAMP_AWAY_TIMEOUT_S", "900"))
IDLE_BRIGHTNESS = float(os.environ.get("LELAMP_IDLE_BRIGHTNESS", "0.20"))

# --- Sensing: Speaker recognition (voice embedding via dlbackend) ---
SPEAKER_RECOGNITION_ENABLED: bool = (
    os.environ.get("LELAMP_SPEAKER_RECOGNITION_ENABLED", "true").lower() == "true"
)
SPEAKER_MIN_AUDIO_S: float = float(os.environ.get("LELAMP_SPEAKER_MIN_AUDIO_S", "0.8")) # seconds
SPEAKER_MATCH_THRESHOLD: float = float(os.environ.get("SPEAKER_MATCH_THRESHOLD", "0.7")) # 0.0 - 1.0
SPEAKER_ENROLL_CONSISTENCY_THRESHOLD: float = float(
    os.environ.get("SPEAKER_ENROLL_CONSISTENCY_THRESHOLD", "0.7")
)
SPEAKER_EMBEDDING_API_TIMEOUT_S: float = float(
    os.environ.get("SPEAKER_EMBEDDING_API_TIMEOUT_S", "15")
)
SPEAKER_UNKNOWN_AUDIO_DIR: str = os.environ.get(
    "LELAMP_UNKNOWN_AUDIO_DIR",
    os.path.join(tempfile.gettempdir(), "lamp-unknown-voice"),
)
DL_SPEAKER_ENDPOINT = os.environ.get("DL_SPEAKER_ENDPOINT", "/lelamp/api/dl/audio-recognizer/embed")
SPEAKER_EMBEDDING_API_URL: str = DL_BACKEND_URL.rstrip("/") + "/" + DL_SPEAKER_ENDPOINT.strip("/") if DL_BACKEND_URL else ""
SPEAKER_EMBEDDING_API_KEY: str = DL_API_KEY

# --- Sensing: Speech emotion recognition (SER via dlbackend) ---
SPEECH_EMOTION_ENABLED: bool = (
    os.environ.get("LELAMP_SPEECH_EMOTION_ENABLED", "true").lower() == "true"
)
SPEECH_EMOTION_FLUSH_S: float = float(
    os.environ.get("LELAMP_SPEECH_EMOTION_FLUSH_S", "10.0")
)
SPEECH_EMOTION_DEDUP_WINDOW_S: float = float(
    os.environ.get("LELAMP_SPEECH_EMOTION_DEDUP_WINDOW_S", "300.0")
)
SPEECH_EMOTION_MIN_AUDIO_S: float = float(
    os.environ.get("LELAMP_SPEECH_EMOTION_MIN_AUDIO_S", "3.0")
)
SPEECH_EMOTION_API_TIMEOUT_S: float = float(
    os.environ.get("LELAMP_SPEECH_EMOTION_API_TIMEOUT_S", "15")
)
DL_SER_ENDPOINT: str = os.environ.get(
    "DL_SER_ENDPOINT", "/lelamp/api/dl/ser/recognize"
)
SPEECH_EMOTION_API_URL: str = (
    DL_BACKEND_URL.rstrip("/") + "/" + DL_SER_ENDPOINT.strip("/")
    if DL_BACKEND_URL else ""
)
SPEECH_EMOTION_API_KEY: str = DL_API_KEY
SPEECH_EMOTION_AUDIO_DIR: str = os.environ.get(
    "LELAMP_SPEECH_EMOTION_AUDIO_DIR",
    os.path.join(tempfile.gettempdir(), "lamp-speech-emotion"),
)
