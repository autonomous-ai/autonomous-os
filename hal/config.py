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
# Ramp before a recording plays: /servo/play interpolates from the current pose
# to the recording's first frame over this many seconds (applies to every
# recording switch — emotions, idle, music groove). Was a hardcoded 5.0 —
# most of the perceived "play is slow" was this pre-roll, not the animation.
# SAFETY.md motion.max_speed still bounds the per-joint speed independently.
SERVO_PLAY_RAMP_S = float(os.environ.get("HAL_SERVO_PLAY_RAMP_S", "2.0"))
HTTP_PORT = int(os.environ.get("HAL_HTTP_PORT", "5001"))
# production (default): bind 127.0.0.1, local-only middleware enforced.
# developer: bind 0.0.0.0, no access restrictions — for local dev/testing only.
_mode = os.environ.get("HAL_MODE", "production").strip().lower()
MODE: str = "developer" if _mode == "developer" else "production"
HTTP_HOST: str = "0.0.0.0" if MODE == "developer" else "127.0.0.1"
CAMERA_INDEX = int(os.environ.get("HAL_CAMERA_INDEX", "0"))
# Optional camera selection by device NAME instead of a bare index (mirrors how
# audio picks devices by hardware name). Case-insensitive substring matched
# against the v4l2 device name (e.g. "OPENAICAM"); resolution prefers the
# stable /dev/v4l/by-id capture symlink so the pick survives index shuffles
# from replug/boot-order. Unset = legacy index behavior. On no match HAL logs
# a warning and falls back to HAL_CAMERA_INDEX.
CAMERA_NAME = os.environ.get("HAL_CAMERA_NAME", "").strip() or None
CAMERA_WIDTH = int(os.environ.get("HAL_CAMERA_WIDTH", "640"))
CAMERA_HEIGHT = int(os.environ.get("HAL_CAMERA_HEIGHT", "480"))
# Camera exposure. Defaults to AUTO: manual exposure with high gain drives the
# camera ISP into an unstable state that corrupts colors (green/magenta
# posterized frames) and sticks for the whole capture session — observed on
# multiple devices with manual+gain 255 and manual+gain 192. Auto has never
# shown the corruption. Trade-off: auto-exposure stretches integration time in
# low light (~60ms), capping delivery at ~16fps at EVERY resolution (not a
# bandwidth limit). Set HAL_CAMERA_AUTO_EXPOSURE=manual to pin exposure for a
# stable frame rate, but keep gain <= ~144 — higher values risk the ISP color
# corruption. exposure_absolute is V4L2 ×100µs: 200=20ms (30fps), 330=33ms
# (≈30fps), 500=50ms (≈20fps).
CAMERA_AUTO_EXPOSURE = os.environ.get("HAL_CAMERA_AUTO_EXPOSURE", "auto").strip().lower()
CAMERA_EXPOSURE = int(os.environ.get("HAL_CAMERA_EXPOSURE", "330"))
# Sensor gain (camera-specific range, e.g. 0–255). Brightens without costing fps
# but adds noise; >~144 risks the ISP color corruption. Applied in manual mode.
CAMERA_GAIN = int(os.environ.get("HAL_CAMERA_GAIN", "96"))
# Optional brightness offset (camera-specific, e.g. -64..64); unset = camera default.
CAMERA_BRIGHTNESS = int(os.environ["HAL_CAMERA_BRIGHTNESS"]) if os.environ.get("HAL_CAMERA_BRIGHTNESS") else None

# --- Audio ---
# Hardware overrides — set in .env to bypass auto-detection
# e.g. HAL_AUDIO_INPUT_ALSA=plughw:1,0  HAL_AUDIO_OUTPUT_ALSA=plughw:2,0
AUDIO_INPUT_ALSA: Optional[str] = os.environ.get("HAL_AUDIO_INPUT_ALSA") or None
AUDIO_OUTPUT_ALSA: Optional[str] = os.environ.get("HAL_AUDIO_OUTPUT_ALSA") or None
# Bluetooth headset profile for "use headset" mode. HFP gives the headset mic
# (mono 16kHz both ways over SCO); off = A2DP stereo playback + built-in mic.
BT_PREFER_HFP: bool = os.environ.get("HAL_BT_PREFER_HFP", "0") == "1"
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
# YOLO falls back to remote YOLOWorld, ~0.55s median, otherwise). Disable to force remote.
TRACKING_FACE_DETECTOR_ENABLED: bool = os.environ.get(
    "HAL_TRACKING_FACE_DETECTOR", "true"
).strip().lower() in ("1", "true", "yes", "on")

# Wall-clock limit for one object-tracking session. Read at HAL startup; set
# HAL_TRACKING_MAX_DURATION_S per device to tune it without changing code.
TRACKING_MAX_DURATION_S: float = float(
    os.environ.get("HAL_TRACKING_MAX_DURATION_S", "10")
)

# --- Data layout ---

# --- Sensing: os-server integration ---
OS_SENSING_URL = "http://127.0.0.1:5000/api/sensing/event"
# Poked after an enrollment DIRECTORY under USERS_DIR appears or disappears
# (/face/remove, /face/reset, /users/rename), so os-server can retire that
# person from every runtime's USER.md straight away instead of leaving a stale
# profile in the agent's system prompt until the next boot. Loopback-only on the
# os-server side. NOT called by /speaker/remove: that drops only the voice/
# subdir and leaves the person enrolled by face.
OS_USER_RECONCILE_URL = "http://127.0.0.1:5000/api/agent/user-reconcile"
# Named-pool filler (os-server owns phrases + language + WAV cache).
OS_SENSING_FILLER_URL = "http://127.0.0.1:5000/api/sensing/filler"
# Publish the captured `look` frame to the Flow Monitor.
#
# Requires the matching `look.capture` handler in os-server — that handler is
# what makes the event monitor-only. Without it the sensing endpoint has no type
# whitelist, so the event falls through to the agent-forward path and injects a
# phantom turn containing the frame path. Set HAL_LOOK_MONITOR=false if HAL is
# ever deployed ahead of os-server.
LOOK_MONITOR_ENABLED: bool = (
    os.environ.get("HAL_LOOK_MONITOR", "true").lower() in ("1", "true", "yes")
)
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
# Floor between two STRANGER-ONLY presence.enter events. Embedding flicker
# mints a fresh stranger_N id every few seconds for the same unrecognizable
# person, and a fresh id is always "new" — without this floor that's an agent
# turn every FACE_COOLDOWN_S (10s). Friend enters are not affected.
FACE_STRANGER_ENTER_FLOOR_S = float(os.environ.get("HAL_FACE_STRANGER_ENTER_FLOOR_S", "300.0"))
FACE_STRANGER_FLUSH_S = float(os.environ.get("HAL_FACE_STRANGER_FLUSH_S", "10.0"))
# An enrolled face can grant voice focus on presence.enter. Keep stranger-only
# enters agent-visible without granting focus unless a deployment explicitly
# opts into guest-first conversation.
PRESENCE_WAKE_STRANGERS: bool = (
    os.environ.get("HAL_PRESENCE_WAKE_STRANGERS", "false").lower()
    in ("1", "true", "yes")
)
# Minimum face bbox HEIGHT as a fraction of frame height. Height, not area:
# area falls off as 1/d^2 while a linear dimension falls off as 1/d, so the
# area form made the knob twice as sensitive for the same change in reach.
# More importantly, yaw (turning the head — the common case) compresses the
# bbox WIDTH while leaving height intact, so an area gate rejected angled
# faces harder than frontal ones at the same distance — fighting the
# extended-set feature that exists to learn those angled views.
FACE_HEIGHT_RATIO_THRESHOLD = float(
    os.environ.get("HAL_FACE_HEIGHT_RATIO_THRESHOLD", "0.10")
)

# --- Sensing: Voice identity (speaker-ID as a presence signal) ---
# How long a confidently matched speaker stays the "current voice user" after
# they last spoke. Deliberately far shorter than FACE_OWNER_FORGET_S (3600s):
# a face keeps proving presence every frame, while a voice proves only that
# someone spoke ONCE at that instant, so the same window would leave a speaker
# "present" long after they left. Only consulted when face has nobody (face
# always wins — see app_state.resolve_current_user).
VOICE_USER_FORGET_S = float(os.environ.get("HAL_VOICE_USER_FORGET_S", "300.0"))

# --- DL backend connection ---
OS_CONFIG_PATH = os.environ.get("OS_CONFIG_PATH", "/root/config/config.json")

# Persisted speaker volume (0-100). set_volume writes it on every change so
# os-server restores the user's last choice at next boot instead of resetting
# to the ROBOT.md startup_volume. Sits next to config.json (the dir shared
# with the Go server via OS_CONFIG_PATH).
VOLUME_STATE_PATH = os.environ.get(
    "HAL_VOLUME_STATE_PATH", os.path.join(os.path.dirname(OS_CONFIG_PATH), ".volume")
)

def _os_cfg_get(key: str, default: str = "") -> str:
    """Read a value from the os-server config.json (shared with the Go server)."""
    try:
        import json
        with open(OS_CONFIG_PATH) as f:
            return json.load(f).get(key, default)
    except Exception:
        return default

def resolve_device_type(default: str = "") -> str:
    """Return the device class (lamp/dog/intern): DEVICE_TYPE env, then config.json.

    Provisioning writes DEVICE_TYPE into /opt/hal/.env and the os-server unit;
    config.json normally carries NO device_type key at all (it is only a manual
    fallback for dev machines). So a bare _os_cfg_get("device_type") resolves to
    the caller's fallback on every provisioned device — anything deriving
    behaviour from the device class must go through here instead. Same order as
    server._resolve_device_type / mic_button._resolve_device_type, without the
    fail-loud: callers here have a usable default.
    """
    dev = os.environ.get("DEVICE_TYPE")
    if dev:
        return dev.strip().lower()
    cfg = _os_cfg_get("device_type")
    if cfg:
        return str(cfg).strip().lower()
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
# Max time to wait for a perception-service WS response (pose/motion frame, heartbeat,
# key exchange). Without this, a non-responding backend blocks the recv() call
# forever, holding a shared perception-pool worker and starving every other
# camera perception (face/light). On timeout the session is dropped + retried.
DL_WS_RECV_TIMEOUT_S = float(os.environ.get("HAL_DL_WS_RECV_TIMEOUT_S", "15.0"))
# Append-only file that records every perception-service WS stall (recv timeout) so the
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

# --- Sensing: Motion detection (action recognition via perception-service) ---
MOTION_ENABLED = os.environ.get("HAL_MOTION_ENABLED", "true").lower() == "true"
MOTION_PER_FACE_ENABLED = os.environ.get("HAL_MOTION_PER_FACE_ENABLED", "false").lower() == "true"
MOTION_PER_FACE_DEDUP_WINDOW_S = float(os.environ.get("HAL_MOTION_PER_FACE_DEDUP_WINDOW_S", "300.0"))
MOTION_PER_FACE_SESSION_TTL_S = float(os.environ.get("HAL_MOTION_PER_FACE_SESSION_TTL_S", "30.0"))
MOTION_PER_FACE_MIN_FRAMES = int(os.environ.get("HAL_MOTION_PER_FACE_MIN_FRAMES", "4"))
MOTION_CONFIDENCE_THRESHOLD = float(
    os.environ.get("HAL_MOTION_CONFIDENCE_THRESHOLD", "0.3")
)
MOTION_FLUSH_S = float(os.environ.get("HAL_MOTION_FLUSH_S", "10.0"))
# Same-activity heartbeat: floor between two motion.activity emissions while
# the coarse activity class hasn't changed. A class TRANSITION (computer→eat,
# …) bypasses this floor, so it can sit at habit-tracking resolution instead
# of reaction latency.
MOTION_EVENT_COOLDOWN_S = float(
    os.environ.get("HAL_MOTION_EVENT_COOLDOWN_S", "900.0")
)
# Min gap for the class-transition bypass above. Guards against a flickering
# detection (drink appearing/vanishing every ~10s flush) turning the bypass
# back into the old every-flush spam.
MOTION_TRANSITION_MIN_GAP_S = float(
    os.environ.get("HAL_MOTION_TRANSITION_MIN_GAP_S", "60.0")
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

# --- Sensing: Emotion detection (face emotion via perception-service) ---
EMOTION_ENABLED = os.environ.get("HAL_EMOTION_ENABLED", "true").lower() == "true"
EMOTION_CONFIDENCE_THRESHOLD = float(
    os.environ.get("HAL_EMOTION_CONFIDENCE_THRESHOLD", "0.5")
)
EMOTION_FLUSH_S = float(os.environ.get("HAL_EMOTION_FLUSH_S", "10.0"))
EMOTION_DEDUP_WINDOW_S = float(os.environ.get("HAL_EMOTION_DEDUP_WINDOW_S", "300.0"))
# How long `thinking` may stay on continuously before the device falls back to
# idle. `thinking` is the only face nothing clears on its own: it is set at the
# start of a wait and overwritten by whatever the reply expresses, so a turn
# that dies mid-flight (realtime exception, delegate that never answers) leaves
# it burning forever with no user input to break it out. 0 disables the net.
#
# 25s comes from device logs (lamp-0c89, 2026-08-19), not from feel. Two kinds
# of turn hold `thinking` legitimately: a realtime in-session reply clears it in
# 0.4-8.6s (n=15, p50 6.5), and a delegated turn runs until the main agent
# answers — event-forwarded → assistant-turn-done measured 6-22s over 5 days
# (n=21, p95 21). So the window has to clear 22s or it would blink idle in the
# middle of a live delegate; 25 is that ceiling plus a small margin. Cutting
# early is cheap (the real emotion re-sets on arrival), so the margin stays thin
# rather than doubling the stuck time when the net actually fires.
EMOTION_THINKING_RESET_S = float(
    os.environ.get("HAL_EMOTION_THINKING_RESET_S", "25.0")
)
EMOTION_SNAPSHOT_DIR = os.environ.get(
    "HAL_EMOTION_SNAPSHOT_DIR",
    os.path.join(tempfile.gettempdir(), "hal-emotion-snapshots"),
)
EMOTION_SNAPSHOT_MAX_COUNT = int(os.environ.get("HAL_EMOTION_SNAPSHOT_MAX_COUNT", "100"))
# Per-trigger debug capture for face emotion: every recognizer trigger writes
# its own timestamped folder (input crop + clean frame + annotated frame +
# result.json) under EMOTION_LOG_DIR, named "<time>_<Emotion>_<conf>" (or
# "<time>_FAIL-<reason>"), so a misclassification is spottable from the
# directory listing alone. Debug aid — off-device analysis, not a runtime input.
# Off by default: this writes four files per face per tick and is a debugging
# aid, not a runtime input. Turn it on with HAL_EMOTION_DEBUG_LOG_ENABLED=true
# when investigating misclassifications. With it off the capture path returns
# before any frame is copied, so it costs nothing.
EMOTION_DEBUG_LOG_ENABLED = (
    os.environ.get("HAL_EMOTION_DEBUG_LOG_ENABLED", "false").lower() == "true"
)
EMOTION_LOG_DIR = os.environ.get(
    "HAL_EMOTION_LOG_DIR",
    "/opt/hal/drivers/sensing/perceptions/processors/emotion-logs",
)
# Cap on retained trigger folders (oldest pruned first); 0 = unbounded.
EMOTION_LOG_MAX_TRIGGERS = int(os.environ.get("HAL_EMOTION_LOG_MAX_TRIGGERS", "500"))

# --- Sensing: Fire hazard detection (object detection via perception-service) ---
FIRE_HAZARD_ENABLED = os.environ.get("HAL_FIRE_HAZARD_ENABLED", "true").lower() == "true"
# Min gap between two detection API calls. The old default 0 disabled the
# gate entirely — one OWLv2 call per sensing tick (~2s), ~43k calls/day.
# 5s still confirms a hazard within FIRE_HAZARD_CONFIRM_S+5s worst case.
FIRE_HAZARD_CHECK_INTERVAL_S = float(os.environ.get("HAL_FIRE_HAZARD_CHECK_INTERVAL_S", "5.0"))
FIRE_HAZARD_CONFIDENCE_THRESHOLD = float(os.environ.get("HAL_FIRE_HAZARD_CONFIDENCE_THRESHOLD", "0.3"))
FIRE_HAZARD_OVERLAP_THRESHOLD = float(os.environ.get("HAL_FIRE_HAZARD_OVERLAP_THRESHOLD", "0.2"))
FIRE_HAZARD_CONFIRM_S = float(os.environ.get("HAL_FIRE_HAZARD_CONFIRM_S", "10.0"))
# Per-TYPE re-alert heartbeat. A NEW hazard type still alerts immediately (no
# dedup entry); this only paces re-reports of the SAME steady hazard — at 120s
# a dinner candle cost 30 agent turns/hour, at 1800s it's 2.
FIRE_HAZARD_DEDUP_WINDOW_S = float(os.environ.get("HAL_FIRE_HAZARD_DEDUP_WINDOW_S", "1800.0"))
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

# --- Sensing: Pose estimation + ergonomic assessment (via perception-service) ---
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
# samples (perception-service missed most frames, presence flicker, etc.), skip the
# inject. Statistical confidence is too low to nag the user.
POSE_WINDOW_MIN_SAMPLES = int(os.environ.get("HAL_POSE_WINDOW_MIN_SAMPLES", "3"))
# Bad-sample definition: any single region (L or R) at sub-score >= this.
# Catches "head thrust forward, rest of body OK" cases that perception-service's
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
# TEMPORARY WORKAROUND — perception-service's signed_flexion_angle returns the
# opposite sign of its docstring ("Positive = forward flexion"): user
# clearly hunched forward produces angle = -72°, not +72°. Flip on
# receive so the monitor table and JSONL match reality. Revert (set to
# False) the moment perception-service's utils.signed_flexion_angle is fixed
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

# --- Sensing: Speaker recognition (voice embedding via perception-service) ---
SPEAKER_RECOGNITION_ENABLED: bool = (
    os.environ.get("HAL_SPEAKER_RECOGNITION_ENABLED", "true").lower() == "true"
)
SPEAKER_MIN_AUDIO_S: float = float(os.environ.get("HAL_SPEAKER_MIN_AUDIO_S", "0.8")) # seconds
# Identity thresholds are RAW cosine in [-1, 1] — the same unit the face
# pipeline uses (see faceid/recognizer.py). They were previously SCALED cosine
# in [0, 1] under the names SPEAKER_MATCH_THRESHOLD /
# SPEAKER_ENROLL_CONSISTENCY_THRESHOLD; the names changed WITH the unit so a
# stale 0.75 in a device .env can never be silently reread as raw (which would
# stop matching dead). Conversion: raw = 2 * scaled - 1, so the old 0.75
# scaled default is exactly 0.5 raw.
SPEAKER_MATCH_COS: float = float(os.environ.get("SPEAKER_MATCH_COS", "0.5"))
# The same bar also gates a multi-sample enroll batch: each clip must clear it
# against at least one OTHER clip, so outliers are ejected without electing any
# clip as a reference. Single-sample enrolls skip the check.
# A confidently-matched utterance only joins a user's extended set when its max
# cosine to their existing samples is BELOW this — anything above is a
# near-duplicate of a sample we already hold. Must stay ABOVE
# SPEAKER_MATCH_COS: both gates measure the same quantity, so the admission
# band is (SPEAKER_MATCH_COS, SPEAKER_DIVERSITY_COS].
SPEAKER_DIVERSITY_COS: float = float(os.environ.get("SPEAKER_DIVERSITY_COS", "0.7"))
# Auto-captured extended samples kept per user, on top of their untouched
# enrollment samples. This is a SAFETY cap, not a disk-space one: retrieval is
# max-over-rows, so every extra row raises every speaker's score and with it
# the false-accept rate.
SPEAKER_MAX_EXTENDED_SAMPLES: int = int(
    os.environ.get("SPEAKER_MAX_EXTENDED_SAMPLES", "3")
)
# Same cap, applied per unknown-voice cluster.
SPEAKER_MAX_CLUSTER_SAMPLES: int = int(
    os.environ.get("SPEAKER_MAX_CLUSTER_SAMPLES", "3")
)
# WAVs kept in a voice_<N>/ cluster dir, oldest evicted first. Distinct from
# SPEAKER_MAX_CLUSTER_SAMPLES, which caps a cluster's *embeddings*: every
# unknown turn's audio is filed here, so without a file cap a recurring
# stranger's directory grows forever (cluster eviction only fires past 50
# DISTINCT voices, which a household never reaches).
#
# Sized for two jobs at once. These files are the material an agent enrols a
# stranger from, so too low weakens deferred enrollment; but each claimed clip
# that survives the enroll gate becomes a PERMANENT anchor row, so this is also
# the only bound on how many rows one deferred enrollment can add. Ten clips is
# roughly 10-30 s of speech at typical turn lengths.
SPEAKER_MAX_CLUSTER_FILES: int = int(
    os.environ.get("HAL_MAX_CLUSTER_FILES", "10")
)
# Extra bars an utterance must clear to extend a user's set, on top of matching.
# A turn's audio can carry the TV, a second speaker, or the device's own TTS
# tail, so extending demands more than recognizing does.
SPEAKER_EXTEND_MIN_DURATION_SEC: float = float(
    os.environ.get("SPEAKER_EXTEND_MIN_DURATION_SEC", "2.0")
)
SPEAKER_EXTEND_MIN_MARGIN_COS: float = float(
    os.environ.get("SPEAKER_EXTEND_MIN_MARGIN_COS", "0.05")
)
SPEAKER_EMBEDDING_API_TIMEOUT_S: float = float(
    os.environ.get("SPEAKER_EMBEDDING_API_TIMEOUT_S", "15")
)
# Every recognize() logs its utterance to the root of SPEAKER_UNKNOWN_AUDIO_DIR
# — on a match too, deliberately, so there is always a recent record of what the
# device heard and a stable path a skill can reuse for a follow-up enroll.
#
# Despite the directory's name this cap is mostly about KNOWN speakers: an
# unrecognized turn is moved out into a voice_<N>/ sub-dir, so what accumulates
# in the root is recognized-user audio plus gate-reject/error clips. It counts
# incoming_*.wav in the root only, hence the name.
#
# Rolling log: keep the newest N, evict oldest-first. ~32 KB per audio-second,
# so a 4 s turn is ~128 KB and 100 files ≈ 13 MB.
# 0 disables the cap (unbounded growth — not recommended on a device).
SPEAKER_MAX_INCOMING_FILES: int = int(
    os.environ.get("HAL_MAX_INCOMING_FILES", "100")
)
SPEAKER_UNKNOWN_AUDIO_DIR: str = os.environ.get(
    "HAL_UNKNOWN_AUDIO_DIR",
    os.path.join(tempfile.gettempdir(), "hal-unknown-voice"),
)
DL_SPEAKER_ENDPOINT = os.environ.get("DL_SPEAKER_ENDPOINT", "/hal/api/dl/audio-recognizer/embed")
SPEAKER_EMBEDDING_API_URL: str = DL_BACKEND_URL.rstrip("/") + "/" + DL_SPEAKER_ENDPOINT.strip("/") if DL_BACKEND_URL else ""
SPEAKER_EMBEDDING_API_KEY: str = DL_API_KEY

# --- Sensing: Speaker recognition — on-device audio preprocessing ---
SPEAKER_PROC_TARGET_SR: int = int(os.environ.get("HAL_SPEAKER_PROC_TARGET_SR", "16000"))
SPEAKER_PROC_ENABLE_MONO: bool = (
    os.environ.get("HAL_SPEAKER_PROC_ENABLE_MONO", "true").lower() == "true"
)
SPEAKER_PROC_ENABLE_RESAMPLE: bool = (
    os.environ.get("HAL_SPEAKER_PROC_ENABLE_RESAMPLE", "true").lower() == "true"
)
SPEAKER_PROC_ENABLE_HIGH_PASS: bool = (
    os.environ.get("HAL_SPEAKER_PROC_ENABLE_HIGH_PASS", "false").lower() == "true"
)
SPEAKER_PROC_HIGH_PASS_CUTOFF_HZ: float = float(
    os.environ.get("HAL_SPEAKER_PROC_HIGH_PASS_CUTOFF_HZ", "80.0")
)
SPEAKER_PROC_ENABLE_NOISE_REDUCE: bool = (
    os.environ.get("HAL_SPEAKER_PROC_ENABLE_NOISE_REDUCE", "false").lower() == "true"
)
SPEAKER_PROC_NOISE_STATIONARY: bool = (
    os.environ.get("HAL_SPEAKER_PROC_NOISE_STATIONARY", "false").lower() == "true"
)
# VAD stage. Backed by TEN-VAD (`hal/drivers/voice/ten_vad_lite`, numpy +
# onnxruntime, original FP32 model) — it replaced torch silero-vad here.
SPEAKER_PROC_ENABLE_VAD: bool = (
    os.environ.get("HAL_SPEAKER_PROC_ENABLE_VAD", "true").lower() == "true"
)
SPEAKER_PROC_VAD_MIN_DURATION_SEC: float = float(
    os.environ.get("HAL_SPEAKER_PROC_VAD_MIN_DURATION_SEC", "0.5")
)
# 0.25, down from the silero-era 0.4. The speaker-band / level gates below remove
# non-speech from INSIDE the kept span, which splits segments and mechanically
# lowers this ratio — at 0.4 the TEN-VAD stage rejected clips holding plenty of
# speech. Raise it only together with disabling those gates.
SPEAKER_PROC_VAD_MIN_VOICE_RATIO: float = float(
    os.environ.get("HAL_SPEAKER_PROC_VAD_MIN_VOICE_RATIO", "0.25")
)
# TEN-VAD speech-probability threshold used to detect (and trim to) speech. Onset
# triggers at this value, offset at (threshold - 0.15) — the same hysteresis
# silero used, so this knob keeps its meaning across the swap. Higher = segments
# close sooner = more aggressive trailing/leading silence trimming. 0.5 is
# TEN-VAD's measured operating point (a sweep put best F1 at 0.45-0.5), and the
# false-positive gates below now do the tail-trimming that the silero-era 0.6
# was raised for. Raise toward 0.6-0.7 for very noisy rooms; lower to 0.45 if
# quiet talkers get clipped.
SPEAKER_PROC_VAD_SPEECH_PROB_THRESHOLD: float = float(
    os.environ.get("HAL_SPEAKER_PROC_VAD_SPEECH_PROB_THRESHOLD", "0.5")
)
# TEN-VAD false-positive suppression (no silero equivalent). Both gates zero out
# VAD frames that are not the clip's dominant speaker before segmentation: the
# band gate keeps only frames inside the clip's own pitch band, the level gate
# drops frames far below the clip's own speech level. They are a pair — the band
# gate cannot reject uniformly loud noise, the level gate cannot reject a loud
# transient. Together they raise the share of the kept span that is really speech
# from ~0.67 to ~0.79 at the cost of recall (0.98 -> 0.76), which is the right
# trade for a recogniser: a clean 2 s beats a dirty 6 s.
#
# They assume ONE dominant speaker per clip — true for recognition, wrong for
# long-form multi-speaker audio. Set SPEAKER_BAND=false and MAX_LEVEL_DROP_DB
# empty for plain TEN-VAD (and then raise MIN_VOICE_RATIO back toward 0.4).
SPEAKER_PROC_VAD_SPEAKER_BAND: bool = (
    os.environ.get("HAL_SPEAKER_PROC_VAD_SPEAKER_BAND", "true").lower() == "true"
)
# Empty string disables the level gate (the library's `None`).
_vad_max_level_drop_db_raw: str = os.environ.get(
    "HAL_SPEAKER_PROC_VAD_MAX_LEVEL_DROP_DB", "20.0"
).strip()
SPEAKER_PROC_VAD_MAX_LEVEL_DROP_DB: Optional[float] = (
    float(_vad_max_level_drop_db_raw) if _vad_max_level_drop_db_raw else None
)
SPEAKER_PROC_ENABLE_RMS_NORMALIZE: bool = (
    os.environ.get("HAL_SPEAKER_PROC_ENABLE_RMS_NORMALIZE", "true").lower() == "true"
)
SPEAKER_PROC_RMS_TARGET: float = float(
    os.environ.get("HAL_SPEAKER_PROC_RMS_TARGET", "0.1")
)
# STOI intelligibility gate (SQUIM-STOI ONNX) — rejects noisy / broken-voice
# audio before it reaches the embedding server. Runs after VAD, once per
# utterance; ~20 MB model loaded once. Chunked by CHUNK_SEC + mean-aggregated to
# bound memory on long clips. The ~20 MB weight is NOT committed — it downloads
# on first use from the CDN into /root/local/models (same convention as the pose
# / faceid weights); if it can't be resolved the gate is skipped with a warning.
#
# OFF by default. It rejects a real speaker often enough to hurt: on device it
# dropped ordinary utterances as FAIL-low-stoi, and a rejected turn has no
# speaker at all, which is worse than a lower-confidence identification the
# recogniser's own thresholds can still weigh. Set
# HAL_SPEAKER_PROC_ENABLE_STOI=true to gate on intelligibility where the room is
# noisy enough for that trade to pay off.
SPEAKER_PROC_ENABLE_STOI: bool = (
    os.environ.get("HAL_SPEAKER_PROC_ENABLE_STOI", "false").lower() == "true"
)
SPEAKER_PROC_STOI_MODEL_PATH: str = os.environ.get(
    "HAL_SPEAKER_PROC_STOI_MODEL_PATH",
    "/root/local/models/squimm_stoi.onnx",
)
SPEAKER_PROC_STOI_THRESHOLD: float = float(
    os.environ.get("HAL_SPEAKER_PROC_STOI_THRESHOLD", "0.70")
)
SPEAKER_PROC_STOI_CHUNK_SEC: float = float(
    os.environ.get("HAL_SPEAKER_PROC_STOI_CHUNK_SEC", "5.0")
)

# --- Sensing: Speech emotion recognition (SER via perception-service) ---
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
_RT_QWEN: dict = _RT.get("qwen") if isinstance(_RT.get("qwen"), dict) else {}


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
REALTIME_PROVIDER: str = _rt_str("HAL_REALTIME_PROVIDER", _RT.get("provider"), "gemini")  # none | gemini | openai | qwen
# When enabled, do not send a voice turn to the realtime agent until an STT
# interim transcript starts with one of the configured wake phrases. This is a
# top-level config.json setting because it also gates the non-realtime Go path.
WAKEWORD_ENABLED: bool = _os_cfg_get("wakeword", False) is True
# Once a wake-word command has been accepted, allow a short sequence of
# follow-up turns without repeating the phrase. Set to 0 to require the wake
# phrase for every mic session even when WAKEWORD_ENABLED is true.
WAKEWORD_FOLLOWUP_TIMEOUT_S: float = max(
    0.0, float(os.environ.get("HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S", "20"))
)
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
# Silent-turn watchdog for turns where a `look` fired. Gemini 3.1's forced
# thinking over a text-dense frame ("read this label") stays silent >8s with
# zero output events — the default watchdog killed such turns seconds before
# the answer (device-observed 2026-07-06). Applies per-turn via
# agent.extend_recv_timeout(); normal turns keep the tight default above.
#
# Raising this also delays the look-frame handoff, which only happens once this
# watchdog gives up: keep REALTIME_GEMINI_VISION_HANDOFF_MAX_AGE_S comfortably
# above it, or the frame expires before it can be handed off (that regression
# ran from 2026-07-06 to 2026-08-24 — see that setting's comment).
# Ceiling on how long ONE turn may stay silent while the server is still
# talking to us. The gap watchdog above ends a turn that produces no output,
# which is right for a model that chose not to answer and wrong for one that is
# busy — a Google Search grounding emits nothing until the search returns, and
# ending the turn there hands a question the model was about to answer to the
# main agent instead (minutes, not seconds), while the abandoned search is
# billed anyway and its chunks still land in the session context.
#
# receive() therefore keeps a turn alive past the gap window as long as inbound
# messages keep arriving (see note_server_activity) — this is the hard stop on
# that, so a server that chatters without ever producing output still cannot
# hang the turn forever. Must stay above the slowest grounded turn worth
# waiting for; measured on lamp-0c89 04/09/2026 a search landed 9.5s into the
# turn. 0 disables the keep-alive entirely (back to the plain gap watchdog).
REALTIME_TURN_MAX_SILENCE_S: float = float(
    os.environ.get("HAL_REALTIME_TURN_MAX_SILENCE_S", "20.0")
)
# Dump every field of Gemini's grounding_metadata verbatim, once per grounded
# turn. Diagnostic only, and verbose — it exists to settle whether a turn that
# logs chunks=0 got an empty search or a stripped payload. Off by default.
REALTIME_GROUNDING_DEBUG: bool = os.environ.get(
    "HAL_REALTIME_GROUNDING_DEBUG", "false"
).lower() in ("1", "true", "yes")
REALTIME_LOOK_RECV_TIMEOUT_S: float = float(
    os.environ.get("HAL_REALTIME_LOOK_RECV_TIMEOUT_S", "20.0")
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
# session then drops that accumulation. Native-audio Gemini skips this POST-turn
# policy when its pre-turn transport recycle already made the session fresh for the
# same idle gap. Long-term continuity survives — the rebuild reloads the persisted
# summary.md. 0 disables. Default 240s (4 min). See RealtimeOrchestrator._mark_turn_start.
REALTIME_SESSION_IDLE_RESET_S: float = float(
    os.environ.get("HAL_REALTIME_SESSION_IDLE_RESET_S", "240")
)
# Must stay BELOW the shortest observed idle death, or the recycle fires too late
# and the turn still lands on a session Gemini already closed. Measured idle gaps
# before a WS 1008 on 2026-08-21: 86s, 98s, 150s, 150s, 185s -- so 120s was inside
# the failure range. 60s clears the 86s floor with margin. The cost is bounded: this
# only fires on a turn that FOLLOWS a long silence, and voice_service buffers the
# audio across the ~1s handshake (see `rebuilding`), so nothing is dropped.
REALTIME_GEMINI_PRE_TURN_RECYCLE_S: float = float(
    os.environ.get("HAL_GEMINI_PRE_TURN_RECYCLE_S", "60")
)
# Close an idle Gemini session ourselves instead of letting the server close it.
# An idle session is killed upstream with WS 1008 "The operation was aborted"
# (measured idle lifetimes: 86s, 98s, 150s, 151s, 152s, 185s, 198s). That close is
# harmless to turns -- the pre-turn recycle above already replaces the session
# before any post-idle turn streams audio -- but the backend logs it as an error
# and pages the dev channel, so the device must not provoke it. Parking closes the
# transport after this many seconds without turn activity and leaves the
# orchestrator `available`: the next turn's prepare_turn() connects a fresh session
# synchronously (voice_service buffers audio across the ~1s handshake), which is
# exactly what the pre-turn recycle would have done for that turn anyway. Must stay
# BELOW the shortest observed idle death (86s); 45s keeps a wide margin. 0 disables.
REALTIME_GEMINI_IDLE_PARK_S: float = float(
    os.environ.get("HAL_GEMINI_IDLE_PARK_S", "45")
)
# Gemini 1011 recovery: how many times to reconnect a FRESH session and replay
# the just-captured turn audio when a turn produced no output (the campaign-api
# proxy drops idle 2.5-native-audio sessions → a post-pause turn lands on a dead
# session → WS 1011). Replaying immediately turns it into an active turn, which
# the proxy serves reliably. 0 disables.
REALTIME_GEMINI_TURN_RETRIES: int = int(
    os.environ.get("HAL_GEMINI_TURN_RETRIES", "2")
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
# Hard gate: never commit an empty-STT turn to the realtime model. The Silero
# guards above (REQUIRE_SPEECH_ON_EMPTY_STT + NOISE_SPEECH_RATIO) only reject
# NON-speech; real human speech that sits close to the mic is voiced (ratio
# >=0.64) and passes them even when nova-3 produced no transcript (short <~2s
# utterances are below nova-3's floor). Committing that raw audio makes Gemini
# fill the silence — it invents a generic greeting, often with a wrong name
# ("Dạ em nghe, anh ... cần gì không?") that nobody said. Since a spoken reply
# can't be retracted, treat "no transcript" as "don't speak". When true, ANY
# empty-STT turn is dropped regardless of duration/voicing. Trade-off: a short
# utterance nova-3 misses yields silence (preferred over a wrong reply). Set
# false to fall back to the Silero-gated audio-only path.
REALTIME_REQUIRE_TRANSCRIPT: bool = os.environ.get(
    "HAL_REALTIME_REQUIRE_TRANSCRIPT", "true"
).lower() in ("1", "true", "yes")
# Register the explicit `reject_turn` tool and allow only that tool call to
# suppress the main-agent fallback. A plain silent completion, timeout, or error
# still falls back as before. Set false to turn this experimental AI filter off.
REALTIME_AI_REJECT_FILTER: bool = os.environ.get(
    "HAL_REALTIME_AI_REJECT_FILTER", "true"
).lower() in ("1", "true", "yes")
# Noise guard for turns that DO have a transcript. The guards above only run when
# STT came back empty, so a noise turn whose STT invented a word ("Ừ", "Okay",
# "Thank you" — nova-3 reports confidence 1.0 for these) bypasses every check and
# commits. Backend telemetry sees those as turns of pure noise. Fabrications are
# short, so when a transcript has at most this many words the Silero voiced-ratio
# guard runs on it too, and the turn is dropped if the audio was not speech. A
# real short command ("bật đèn") is voiced and still passes. 0 disables the check;
# raising it puts longer real utterances at the mercy of the voiced-ratio floor.
REALTIME_NOISE_GUARD_MAX_WORDS: int = int(
    os.environ.get("HAL_REALTIME_NOISE_GUARD_MAX_WORDS", "3")
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
# Default to 3.1-flash-live. 2.5 native-audio is ~33% cheaper on text tokens
# ($0.50 vs $0.75 /M in, same per-turn usage measured on device), but through the
# campaign-api proxy it returns WS 1011 on a turn that follows an idle pause, so
# it needs the whole idle-workaround set — including the suppressed
# mid-activity [TURN CONTEXT], which silently drops the per-turn speaker identity
# and language reminder (see gemini_needs_idle_workaround() in realtime/config.py).
# 3.1 has neither problem, so every workaround stays off. Switching back to a
# *native-audio* model re-enables them automatically, and also requires the
# language_code-omit fix in gemini_live.py (native-audio rejects an explicit
# language_code). Override via realtime.gemini.model or HAL_GEMINI_LIVE_MODEL.
REALTIME_GEMINI_MODEL: str = _rt_str("HAL_GEMINI_LIVE_MODEL", _RT_GEMINI.get("model"), "gemini-3.1-flash-live-preview")
REALTIME_GEMINI_VOICE: str = _rt_str("HAL_GEMINI_LIVE_VOICE", _RT_GEMINI.get("voice"), "Kore")
REALTIME_GEMINI_SAMPLE_RATE: int = 16000
REALTIME_GEMINI_THINKING_LEVEL: str = _rt_str("HAL_GEMINI_THINKING_LEVEL", _RT_GEMINI.get("thinking_level"), "MINIMAL")
REALTIME_GEMINI_USE_LANGUAGE_CODES: bool = os.environ.get("HAL_GEMINI_USE_LANGUAGE_CODES", "false").lower() in ("1", "true", "yes")
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
# In-session vision: register a `look` tool so Gemini Live captures one camera
# frame and answers "what is this / what do you see" DIRECTLY in the realtime
# session, instead of delegating to main (main → skill lookup → /camera/snapshot
# → vision LLM, several seconds). One frame per call (tool-triggered, NOT a video
# stream) keeps the added token cost marginal. Defaults ON; env HAL_GEMINI_VISION
# or realtime.gemini.vision overrides. When OFF (or no camera / non-Gemini
# provider) the tool isn't registered and visual questions fall back to the old
# delegate flow. Defaults ON; set HAL_GEMINI_VISION=false (or realtime.gemini.vision
# false) to force the old delegate flow. The captured frame is downscaled to
# VISION_MAX_WIDTH before send to bound image tokens.
REALTIME_GEMINI_VISION: bool = (
    os.environ.get(
        "HAL_GEMINI_VISION",
        str(_RT_GEMINI.get("vision", True)),
    ).lower()
    in ("1", "true", "yes")
)
REALTIME_GEMINI_VISION_MAX_WIDTH: int = int(
    os.environ.get("HAL_GEMINI_VISION_MAX_WIDTH", "768")
)
# Aim the head at the subject BEFORE the `look` tool captures. `look` takes no
# parameters and grabs whatever the camera currently sees, so without this the
# model can answer confidently about a wall. Bounded by LOOK_AIM_DEADLINE_S so a
# live turn never stalls: on expiry the capture proceeds from wherever the head
# reached. Yaw only — see hal/drivers/tracking/aim.py for why pitch is excluded.
# Set HAL_LOOK_AIM=false to disable without a rollout if it misbehaves in the field.
LOOK_AIM_ENABLED: bool = (
    os.environ.get("HAL_LOOK_AIM", "true").lower() in ("1", "true", "yes")
)
# Ceiling for the aim, not a cost: it returns the moment the subject is centred,
# so a converged aim never spends this. It only bounds the failure case — the
# head starting far off the subject, where each iteration costs ~1s (detect +
# settle + move) and cutting it short means capturing a frame the subject is not
# in. Device-tuned 2026-08-19: 0.8s allowed a single iteration and every look
# that began off-centre timed out mid-correction, capturing a blurred, uncentred
# frame. Long enough to converge beats short enough to fail fast.
#
# The dead-air filler (REALTIME_FILLER_DELAY_S, 1.5s) covers the wait when it
# does run long.
LOOK_AIM_DEADLINE_S: float = float(
    os.environ.get("HAL_LOOK_AIM_DEADLINE_S", "8")
)
# Horizontal field of view used ONLY to convert the subject's pixel offset into
# degrees of yaw for the look-aim. Deliberately separate from
# tracking/constants.py CAMERA_FOV_DEG (60.0), which the object tracker is tuned
# around — correcting the shared constant would silently re-tune tracking too.
#
# 60 was a guess and it is roughly half the truth, which made every aim step
# remove only ~46% of the error: device traces put the real lens at 107-123 deg
# (2026-08-19, measured as head-degrees-moved per frame-fraction the subject
# shifted). 100 is set slightly BELOW the measurement on purpose — the lens is a
# fisheye, so the mapping is non-linear and compressed at the edges, and
# undershooting converges monotonically while overshooting oscillates.
LOOK_AIM_FOV_DEG: float = float(
    os.environ.get("HAL_LOOK_AIM_FOV_DEG", "100.0")
)
# Confidence floor for the aim's own person/face lookup. The detector's global
# DETECT_MIN_CONFIDENCE is 0.15 — deliberately loose, tuned so the TRACKER keeps
# its lock on a phone at an odd angle, where a miss costs more than a false
# positive. Aiming wants the opposite trade: a false positive turns the lamp at
# a wall (device 2026-08-19 — a person rendered inside a laptop screen was
# accepted and aimed at). Raised here only, leaving the tracker's floor alone.
#
# Applies to detections that report a confidence; the YuNet face path enforces
# its own threshold instead.
LOOK_AIM_MIN_CONFIDENCE: float = float(
    os.environ.get("HAL_LOOK_AIM_MIN_CONFIDENCE", "0.5")
)
# Minimum apparent size for a detection to count as "the person talking to us".
# Expressed as a fraction of FRAME HEIGHT: a close subject is often clipped
# left/right, but their height still scales with distance. Device-measured on
# 1280x720 — a far colleague reads ~0.10 and a spurious far face ~0.035, while
# the actual asker reads ~0.23.
LOOK_AIM_MIN_PERSON_HEIGHT_FRAC: float = float(
    os.environ.get("HAL_LOOK_AIM_MIN_PERSON_HEIGHT_FRAC", "0.15")
)
# Faces are a much smaller box than a whole person at the same distance, so
# they get their own, lower floor.
LOOK_AIM_MIN_FACE_HEIGHT_FRAC: float = float(
    os.environ.get("HAL_LOOK_AIM_MIN_FACE_HEIGHT_FRAC", "0.08")
)
# Passive bearing sampling. Without it the estimate only learns from visual
# questions that happen to end near-perfectly centred — 2 samples in a full day
# of device testing, against a 6h confidence half-life, so it decayed faster
# than it learned. This watches for a NEARBY person on a slow cadence and folds
# in what it sees, so the lamp knows where its user usually is without being
# asked anything.
BEARING_SAMPLE_ENABLED: bool = (
    os.environ.get("HAL_BEARING_SAMPLE", "true").lower() in ("1", "true", "yes")
)
# 5 minutes: eight samples reaches full confidence inside an hour of presence,
# and one detector inference per 5 min is a rounding error on the CPU.
BEARING_SAMPLE_INTERVAL_S: float = float(
    os.environ.get("HAL_BEARING_SAMPLE_INTERVAL_S", "300")
)
# The subject may be off-centre horizontally — their bearing is recovered as
# yaw + dx x scale — but only so far, because that correction leans on the FOV
# constant the aim exists to avoid trusting.
BEARING_SAMPLE_MAX_DX_FRAC: float = float(
    os.environ.get("HAL_BEARING_SAMPLE_MAX_DX_FRAC", "0.25")
)
# Save what each bearing sample saw, box drawn on, under
# SNAPSHOT_PERSIST_DIR/sensing_bearing/. Servable by
# GET /api/sensing/snapshot/sensing_bearing/<name>, so the samples can be
# reviewed without SSH.
BEARING_SNAPSHOT_ENABLED: bool = (
    os.environ.get("HAL_BEARING_SNAPSHOT", "true").lower() in ("1", "true", "yes")
)
# Oldest are evicted past this. One frame per sample interval accumulates
# quietly forever otherwise.
BEARING_SNAPSHOT_KEEP: int = int(
    os.environ.get("HAL_BEARING_SNAPSHOT_KEEP", "30")
)
# --- Gaze wake: turning toward the lamp as a third way to address it ----------
#
# A desk lamp sits an arm's length from its user, so "hey <name>" ten times a
# day reads as talking to an appliance, and a button press reads as operating
# one. Between two people the cue is neither: you turn toward someone and speak.
#
# This adds that as a THIRD opener of the existing wake gate, alongside the
# spoken wake phrase and the single click. It does not replace either, and it
# does not touch what happens after the gate opens. On a device without a
# camera it simply never arms, leaving the other two openers untouched.
#
# It is inert when WAKEWORD_ENABLED is false: with no wake word, every utterance
# already dispatches, so there is no gate left to open.
GAZE_WAKE_ENABLED: bool = (
    os.environ.get("HAL_GAZE_WAKE", "false").lower() in ("1", "true", "yes")
)
# Log the decision without acting on it. ON by default so the thresholds below
# can be chosen from measurements taken beside a real user, rather than guessed:
# nobody knows what angle reads as "addressing the lamp" until it is counted.
# Shadow runs cost nothing — no turn is opened, so no LLM or TTS is spent.
GAZE_WAKE_SHADOW: bool = (
    os.environ.get("HAL_GAZE_SHADOW", "true").lower() in ("1", "true", "yes")
)
# How far the head may be turned off the lamp and still count as facing it.
# Device-observed reference frames: a near-profile head (one ear visible, both
# eyes not) measures well past 60 deg and must NOT open the gate — that is the
# posture of someone talking to a colleague while their torso happens to face
# the desk. A head with both eyes and both lenses visible measures under 25.
GAZE_MAX_YAW_DEG: float = float(os.environ.get("HAL_GAZE_MAX_YAW_DEG", "25"))
# How far back the evidence window reaches from the newest sample. One knob,
# not a separate "hold" and "speech window": both described the same span — the
# moments between turning toward the lamp and starting to speak — and two names
# for one span drift apart. People turn BEFORE they speak, so this reaches
# backwards only. The sole exception is an empty-evidence recovery: VAD asks
# the watcher to restore the remembered pose, then the completed SAME utterance
# is checked once more. A head actually measured facing away never gets that
# exception, so this does not turn general speech into a wake trigger.
#
# 1.5 s is also the smallest window that carries enough samples to vote with. At
# 3 fps a 0.8 s window holds three observations, and three noisy samples cannot
# support a majority the noise cannot flip.
GAZE_WINDOW_S: float = float(os.environ.get("HAL_GAZE_WINDOW_S", "1.5"))
# What fraction of the samples in that window must have seen a facing head.
#
# NOT an unbroken run. Per-sample yaw is noisy: at 640x360 a face filling a
# fifth of the frame leaves ~25 px between the eyes, so a one-pixel landmark
# error moves the angle a long way, and asin amplifies it further near the
# extremes. A device trail of a user plainly facing the lamp read
# [10,15,8,25,36,1,-,90] across two seconds — an impossible amount of real head
# movement, so the variation is measurement, not gesture. Requiring every sample
# to pass rejects that; requiring most of them separates it cleanly from a head
# genuinely turned away, which reads [3,90,90,90,90,90,90,90].
GAZE_MIN_FACING_RATIO: float = float(
    os.environ.get("HAL_GAZE_MIN_FACING_RATIO", "0.6")
)
# Below this many samples in the window there is not enough evidence to call it
# either way, so it is not called. Guards the moments after start-up and after a
# live look monopolised the detector.
#
# 2, not 3. The loop is paced by real work — waiting on a frame, then running
# the detector — so it achieves roughly two samples a second whatever
# GAZE_SAMPLE_FPS asks for, and a 1.5 s window holds two or three. At 3 this
# floor rejected users the rest of the pipeline agreed were facing the lamp:
# `yaw=1.0 face=84px facing=100% of 1 -> skip`, with a trail of seven
# consecutive in-cone samples behind it. Raising the sample rate does not help;
# the ceiling is the work, not the sleep.
GAZE_MIN_SAMPLES: int = int(os.environ.get("HAL_GAZE_MIN_SAMPLES", "2"))
# Minimum face height in PIXELS for a sample to be trusted at all.
#
# In pixels, not as a fraction of the frame, because what this protects is
# landmark precision and that depends on pixels alone. Yaw is recovered from the
# offset between five landmarks; on a face 10 px tall those five points fall
# within about three pixels of each other, so the angle is arithmetic performed
# on rounding error. Device probe, one frame: three background colleagues at
# 8-18 px yielded yaw 49 / 20 / 29 with detector scores 0.43-0.70 — pure noise —
# while the seated user at 78 px and score 0.88 measured 90 and was, correctly,
# in profile. The two populations do not overlap, so this floor removes the
# entire class of garbage rather than tuning against it.
#
# It also subsumes the old frame-fraction floor: anyone far enough away to be a
# bystander is, by construction, too small in pixels.
#
# WHICH pixels: the DOWNSCALED frame the watcher detects on, not the camera's
# own resolution. `_loop` measures on `frame_utils.downscale(frame)`, which
# clamps width to VISION_MAX_WIDTH (640) and returns scale = 640/w — so at
# 1280x720 this 48 px floor is 96 px in the original image, and at 640 or
# narrower it is 48 px in both. Unlike LOOK_AIM_MIN_FACE_HEIGHT_FRAC, which is
# a fraction and immune, this constant silently doubles or halves if
# VISION_MAX_WIDTH or the camera mode changes. The device logs it reads against
# (`face=49px`) are downscaled readings, so the number matches practice today.
GAZE_MIN_FACE_PX: int = int(os.environ.get("HAL_GAZE_MIN_FACE_PX", "48"))
# How much wider the acceptance cone grows for a face at the very edge of frame,
# as a multiple of GAZE_MAX_YAW_DEG. Scales linearly with distance from the
# frame centre; 1.0 disables the compensation.
#
# The yaw estimate assumes a pinhole projection, and this lens is not one — the
# aim was rewritten to stop trusting a fixed FOV for the same reason, and the
# repo cannot even agree on the number (60 deg in constants.py against 78 in the
# hardware BOM) because barrel distortion makes it position-dependent. A face at
# the edge has its landmark geometry stretched, which inflates the angle.
# Device-measured: a user who did not move read [8,9,15,5,12,33,35,28] as their
# face drifted outward, and the tail was refused for a turn that never happened.
GAZE_EDGE_CONE_SCALE: float = float(
    os.environ.get("HAL_GAZE_EDGE_CONE_SCALE", "1.8")
)
# Seconds of yaw history kept. Must exceed GAZE_WINDOW_S or the lookback cannot
# see far enough back to judge the gesture. It briefly had to be at least TWICE
# that, because a transition test read the window before the decision window as
# a baseline; that test is gone, so only the original rule applies. 4.0 is kept
# rather than trimmed back — the extra second costs nothing and the `trail=`
# field in the log reads better with more history behind it.
GAZE_BUFFER_S: float = float(os.environ.get("HAL_GAZE_BUFFER_S", "4.0"))
# Sampling rate of the watcher.
#
# 6, not the 3 that resolving the gesture alone would need. The gesture is slow,
# but the DECISION is a vote, and the vote only counts samples that actually
# measured a head: dropped frames and faces too small to trust are excluded. At
# 3 fps a 1.5 s window held four or five raw samples and often just one to three
# usable ones, which sits right on GAZE_MIN_SAMPLES — so a user facing the lamp
# dead-on was refused for want of evidence (`yaw=4.5 face=49px facing=100% of 1
# -> skip`). Sampling twice as often buys the tally enough votes to be stable.
#
# Affordable because the cost was measured, not assumed: with the watcher
# running, CPU idle went 69.2% -> 68.8% and HAL's own share did not rise above
# its normal range on the 8-core A523. YuNet on a downscaled frame is cheap.
GAZE_SAMPLE_FPS: float = float(os.environ.get("HAL_GAZE_SAMPLE_FPS", "6"))
# Minimum gap between two gaze-opened gates, so a single conversation cannot
# open one per sentence. The follow-up window already covers continuing a turn.
GAZE_COOLDOWN_S: float = float(os.environ.get("HAL_GAZE_COOLDOWN_S", "5"))
# How much floor a gaze wake claims, against WAKEWORD_FOLLOWUP_TIMEOUT_S for the
# wake phrase and the button (60 on lamp).
#
# The window self-extends: every authorised turn with a transcript refreshes it
# (voice_service), so one wrong opener does not cost a turn, it costs a
# conversation — a discussion held near the lamp keeps the lamp in it until a
# gap longer than the window. The wake phrase and the click are deliberate acts
# and keep the full allowance; this one is an inference about where a head was
# pointing, so it claims less. Capped by the default, never above it.
# 10, not 20: WAKEWORD_FOLLOWUP_TIMEOUT_S's own CODE default is 20, so 20 here
# would be no reduction at all on any device that has not overridden it — only
# a lamp with the 60s override would see a difference. The point is a smaller
# allowance everywhere, not just where someone happened to widen the default.
GAZE_WAKE_FOCUS_S: float = float(os.environ.get("HAL_GAZE_WAKE_FOCUS_S", "10"))
# Directory holding the boot-scoped sidecars app_state and routes/scene write
# (LED / mic / speaker / camera / sleep / scene). `/tmp` on a body, so they die
# with the machine but survive a HAL restart — which is the whole point: an OTA
# must not wake a sleeping device or drop the user's colour.
#
# Overridable so a test run gets its own: these files outlive the process, so on
# the shared default one run that ended with the body asleep left every LATER
# run starting asleep, and running the suite on a real body would overwrite that
# body's live switches (observed 03/09/2026).
STATE_DIR: str = os.environ.get("HAL_STATE_DIR", "/tmp")

# --- Simulation (laptop body: `make sim`) ---
#
# SIMULATE is the on/off switch; SIM_MEDIA is what the developer ASKED for.
# What each subsystem actually ends up running is decided at boot and tracked in
# app_state (`sim_media_camera` / `sim_media_audio`), because a host camera or
# mic can be missing, busy, or permission-denied — that is runtime state, not
# configuration, so it stays there.
SIMULATE: bool = os.environ.get("HAL_SIMULATE", "").lower() in ("1", "true", "yes")
SIM_MEDIA: str = os.environ.get("HAL_SIM_MEDIA", "virtual").strip().lower()
# Who a turn belongs to when neither face nor voice has named anyone.
DEFAULT_USER: str = os.environ.get("HAL_DEFAULT_USER", "unknown")
# Where the remembered user bearing lives. NOT a boot sidecar: this must survive
# reboots, unlike the mic/speaker/camera state in app_state.
USER_BEARING_PATH: str = os.environ.get(
    "HAL_USER_BEARING_PATH", "/var/lib/hal/user_bearing.json"
)
# Speak while the aim searches for the user. ON by default: the lamp physically
# turning away mid-question is confusing unless it says why, and that is the one
# aim state that genuinely needs a voice.
LOOK_AIM_SPEAK: bool = (
    os.environ.get("HAL_LOOK_AIM_SPEAK", "true").lower() in ("1", "true", "yes")
)
# Announce the capture itself ("let me see"). ON, but narrow: it fires only when
# the aim actually had to move, so the common case — subject already centred,
# shutter in a few hundred ms — stays silent. That makes it cover the waits
# without narrating every visual question. Set HAL_LOOK_AIM_SPEAK_CAPTURE=false
# to silence it if it turns out to stack awkwardly after a search.
LOOK_AIM_SPEAK_CAPTURE: bool = (
    os.environ.get("HAL_LOOK_AIM_SPEAK_CAPTURE", "true").lower() in ("1", "true", "yes")
)
# Cost guard for `look`: minimum seconds between two image SENDS. A model can call
# look several times in a row (same turn, or back-to-back turns); each new image
# costs vision tokens. Within this window we DON'T capture/send a fresh frame —
# the frame from the recent look is still in the session context, so we just let
# the model answer from it. Set 0 to always send a fresh frame.
REALTIME_GEMINI_VISION_MIN_INTERVAL_S: float = float(
    os.environ.get("HAL_GEMINI_VISION_MIN_INTERVAL_S", "10.0")
)
# Vision handoff: when a `look` turn delegates / falls back to the main agent
# (e.g. Gemini timed out mid-turn), the frame `look` already captured rides the
# sensing POST so the main agent answers from it instead of snapshotting again.
# The frame is only attached if it is younger than this (freshness guard; the
# frame is also cleared per-turn). 0 disables the age guard.
#
# MUST stay ABOVE REALTIME_LOOK_RECV_TIMEOUT_S plus dispatch time. The timeout
# fallback is the main way this handoff is reached, and it only fires after that
# watchdog has run its full course — after which HAL still replays the turn
# audio and runs speaker ID before POSTing. Set the two equal and the guard
# expires every frame it exists to serve: both were 20.0 between 2026-07-06
# (when the look watchdog went 8s -> 20s and nobody adjusted this) and
# 2026-08-24, and lamp-0c89 measured 3/3 look turns falling back at exactly 20s
# with the image dropped, so the vision describe never ran once.
REALTIME_GEMINI_VISION_HANDOFF_MAX_AGE_S: float = float(
    os.environ.get("HAL_GEMINI_VISION_HANDOFF_MAX_AGE_S", "45.0")
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

# --- Realtime: Qwen Omni Realtime (DashScope / Model Studio intl) ---
# Unlike gemini/openai there is NO llm_base_url-derived fallback: Qwen realtime
# talks straight to the Alibaba MaaS host, not through the campaign-api proxy.
# NOTE: deliberately NO fallback to the shared realtime.api_key/base_url — on
# devices those hold the campaign-api credentials (gemini/openai path) and
# would produce a baffling 401 against the Alibaba host. Both values must come
# from env (device /opt/hal/.env: DASHSCOPE_API_KEY, HAL_QWEN_REALTIME_BASE_URL
# = wss://<workspace>.ap-southeast-1.maas.aliyuncs.com/api-ws/v1) or from
# config.json realtime.qwen.{api_key,base_url}; empty → the WS handshake fails
# loudly in the hal log.
REALTIME_QWEN_API_KEY: str = (
    os.environ.get("DASHSCOPE_API_KEY", "")
    or _RT_QWEN.get("api_key", "")
)
REALTIME_QWEN_BASE_URL: str = (
    os.environ.get("HAL_QWEN_REALTIME_BASE_URL", "")
    or _RT_QWEN.get("base_url", "")
)
# Default 3.5-plus: turbo (legacy) NEVER fires function calls and ignores
# [TURN CONTEXT] (device-tested 2026-07-06 — no delegate, no time answers),
# which breaks the whole delegate flow; 3.5-plus delegates cleanly, reads turn
# context, and has built-in web search. Voice: 3.5-plus accepts only
# Serena/Ethan of the QwenVoice set (Cherry/Chelsie are turbo-only, rejected
# with InvalidParameter at first response).
REALTIME_QWEN_MODEL: str = _rt_str("HAL_QWEN_REALTIME_MODEL", _RT_QWEN.get("model"), "qwen3.5-omni-plus-realtime")
REALTIME_QWEN_VOICE: str = _rt_str("HAL_QWEN_REALTIME_VOICE", _RT_QWEN.get("voice"), "Ethan")
# Built-in web search (3.5 models): session.update `enable_search: true`. The
# qwen twin of Gemini's Google Search grounding — public live-data questions
# (news, scores, weather) get answered IN-SESSION with fresh facts instead of
# delegating. Without the flag the model answers from stale knowledge
# (probed 2026-07-06: "no match today" vs the real 2-1 result with it on).
REALTIME_QWEN_SEARCH: bool = (
    os.environ.get(
        "HAL_QWEN_SEARCH",
        str(_RT_QWEN.get("search", True)),
    ).lower()
    in ("1", "true", "yes")
)
REALTIME_QWEN_SAMPLE_RATE: int = 16000

# --- Realtime: Context manager ---
OPENCLAW_WORKSPACE_DIR: str = os.environ.get("HAL_OPENCLAW_WORKSPACE_DIR", "/root/.openclaw/workspace")
HERMES_WORKSPACE_DIR: str = os.environ.get("HAL_HERMES_WORKSPACE_DIR", "/root/.hermes")
# PicoClaw/Codex/Claude Code/OpenCode workspaces mirror OpenClaw's layout (see orchestrator.py maps).
PICOCLAW_WORKSPACE_DIR: str = os.environ.get("HAL_PICOCLAW_WORKSPACE_DIR", "/root/.picoclaw/workspace")
CODEX_WORKSPACE_DIR: str = os.environ.get("HAL_CODEX_WORKSPACE_DIR", "/root/.codex/workspace")
CLAUDECODE_WORKSPACE_DIR: str = os.environ.get("HAL_CLAUDECODE_WORKSPACE_DIR", "/root/.claudecode/workspace")
OPENCODE_WORKSPACE_DIR: str = os.environ.get("HAL_OPENCODE_WORKSPACE_DIR", "/root/.opencode/workspace")

# ACTIVE_AGENT_WORKSPACE_DIR is the ACTIVE runtime's workspace (follows
# AGENT_GATEWAY, like SNAPSHOT_DIR below). Persona files (IDENTITY.md /
# SOUL.md) live per-runtime, so anything reading them OUTSIDE the realtime
# orchestrator (which has its own per-gateway map) must resolve through this —
# a hardcoded openclaw path reads a stale/template IDENTITY.md on other
# runtimes and the agent name silently falls back to the device type (the
# "Lamp" wake-word bug, device-observed 2026-07-08 on claudecode).
_AGENT_WORKSPACE_DIRS: dict[str, str] = {
    "openclaw": OPENCLAW_WORKSPACE_DIR,
    "hermes": HERMES_WORKSPACE_DIR,
    "picoclaw": PICOCLAW_WORKSPACE_DIR,
    "codex": CODEX_WORKSPACE_DIR,
    "claudecode": CLAUDECODE_WORKSPACE_DIR,
    "opencode": OPENCODE_WORKSPACE_DIR,
}
ACTIVE_AGENT_WORKSPACE_DIR: str = _AGENT_WORKSPACE_DIRS.get(
    AGENT_GATEWAY, OPENCLAW_WORKSPACE_DIR
)

# Camera snapshot dir. MUST sit under the ACTIVE agent runtime's media root — the
# agent's image tool only reads files under its allow-list, else it returns "not
# under an allowed directory". So this follows AGENT_GATEWAY instead of a hardcoded
# brand (the multi-agent bug). NOT /tmp: outside the allow-list AND wiped on
# restart. Override with HAL_SNAPSHOT_DIR. The path is handed to the agent
# absolute, so any runtime reads it as root.
_AGENT_CONFIG_DIRS: dict[str, str] = {
    "openclaw": "/root/.openclaw",
    "hermes": "/root/.hermes",
    "picoclaw": "/root/.picoclaw",
    "codex": "/root/.codex",
    "claudecode": "/root/.claudecode",
    "opencode": "/root/.opencode",
}
SNAPSHOT_DIR: str = os.environ.get("HAL_SNAPSHOT_DIR") or (
    _AGENT_CONFIG_DIRS.get(AGENT_GATEWAY, _AGENT_CONFIG_DIRS["openclaw"])
    + "/media/hal-snapshots"
)
# Realtime memory follows the ACTIVE runtime's workspace — memory.jsonl plus
# the derived summary.md / device_summary.md / memory_raw.jsonl all live in its
# realtime/ subdir (context_manager/base.py derives them from this path's
# parent). Pinning this to openclaw meant every runtime shared ONE realtime
# memory: stale facts from an old runtime era (e.g. a previous agent name)
# kept leaking into the current persona's turns (device-observed 2026-07-08).
_rt_workspace: str = ACTIVE_AGENT_WORKSPACE_DIR.rstrip("/")
REALTIME_MEMORY_PATH: str = os.environ.get("HAL_REALTIME_MEMORY_PATH", f"{_rt_workspace}/realtime/memory.jsonl")
REALTIME_MAX_MEMORY_ENTRIES: int = int(os.environ.get("HAL_REALTIME_MAX_MEMORY_ENTRIES", "1000"))
REALTIME_MEMORY_TRIM_KEEP: int = int(os.environ.get("HAL_REALTIME_MEMORY_TRIM_KEEP", "500"))
# These bound the DEVICE MEMORY / REALTIME MEMORY sections of the per-turn floor
# (build_instructions), which Gemini re-bills EVERY turn — this floor is the main
# realtime cost driver (in_text ≈ 9.2k tokens/turn at 16k+16k chars). Capped to
# ~8k chars (~2k tokens) each to roughly halve the floor; the full history is
# preserved in summary.md + memory_raw.jsonl, and the tighter cap also makes
# realtime-memory summarization trigger sooner (fresher in-context memory).
REALTIME_DEVICE_MEMORY_MAX_CHARS: int = int(os.environ.get("HAL_REALTIME_DEVICE_MEMORY_MAX_CHARS", "8000"))
REALTIME_MEMORY_MAX_CHARS: int = int(os.environ.get("HAL_REALTIME_MEMORY_MAX_CHARS", "8000"))
# Cap on the rolling realtime summary.md — part of the per-turn floor, so kept
# tight (~1.5k tokens). Env-overridable for tuning.
REALTIME_SUMMARY_MAX_CHARS: int = int(os.environ.get("HAL_REALTIME_SUMMARY_MAX_CHARS", "5000"))
# Ceiling for the SOUL+IDENTITY+USER.md identity section of the realtime floor.
# USER.md/IDENTITY.md are agent-writable, so without this the per-turn floor
# grows unbounded. Default leaves today's ~9.6k chars untouched.
REALTIME_IDENTITY_MAX_CHARS: int = int(os.environ.get("HAL_REALTIME_IDENTITY_MAX_CHARS", "12000"))
# Cap on the [REPLY] transcript replayed to the MAIN agent after a
# realtime-handled turn. The replay exists for memory continuity (the main
# agent burns a full turn just to record it + answer NO_REPLY), so the gist is
# enough — an uncapped long spoken reply inflates that already-overhead turn.
REALTIME_REPLY_SYNC_MAX_CHARS: int = int(os.environ.get("HAL_REALTIME_REPLY_SYNC_MAX_CHARS", "600"))
# Cap on each [TTS HISTORY] line pushed into the live Gemini session after the
# device speaks. It accumulates in session context and is re-billed on every
# later turn until recycle; Gemini only needs the gist to avoid repeating
# itself.
REALTIME_TTS_HISTORY_MAX_CHARS: int = int(os.environ.get("HAL_REALTIME_TTS_HISTORY_MAX_CHARS", "300"))
# Dead air while the realtime model works on a committed turn. After this many
# seconds with no output yet, HAL asks os-server to speak one opening filler
# ("one sec", "let me check"); the model's own first sentence interrupts it.
# 0 disables.
#
# The 1.5s default assumes a chit-chat answer starts in ~1s, so the filler only
# covers the slow class (a turn grounded with Google Search emits no token until
# the search returns). That assumption is per-model, and is already false on at
# least one shipped body: measured on lamp-0c89 26/08/2026 against
# gemini-3.1-flash-live-preview behind the campaign-api proxy, EVERY turn took
# 3.0-8.0s to its first sentence (median 4.0s, n=31). There the filler is not
# covering an outlier, it is the only thing the user hears for the first few
# seconds, and the body lowers this in its own .env. Tune it per device from
# measured time-to-first-sentence, not from this default.
REALTIME_FILLER_DELAY_S: float = float(os.environ.get("HAL_REALTIME_FILLER_DELAY_S", "1.5"))

# Time-to-first-audio, text (non-native-audio) path only. Sentences are streamed
# to TTS as they complete, so the first thing the user hears waits for a full
# sentence terminator — and the model's opening sentence is often long. Below
# this many characters the first utterance of a turn may instead be cut at a
# CLAUSE boundary (comma / semicolon / colon) and spoken immediately, with the
# remainder queued behind it; TTS is a queue, so the reply still comes out in
# order and the split lands where a speaker would breathe anyway.
#
# Only ever applies to the FIRST chunk of a turn — that is the only one whose
# latency the user is sitting in silence for. 0 disables (wait for the full
# sentence, the pre-04/09/2026 behaviour).
REALTIME_FIRST_CHUNK_MAX_CHARS: int = int(
    os.environ.get("HAL_REALTIME_FIRST_CHUNK_MAX_CHARS", "90")
)

# --- Realtime: Summarizer (Anthropic Messages API) ---
REALTIME_SUMMARIZER_ENABLED: bool = os.environ.get("HAL_REALTIME_SUMMARIZER_ENABLED", "true").lower() in ("1", "true", "yes")
REALTIME_SUMMARIZER_API_KEY: str = os.environ.get("HAL_REALTIME_SUMMARIZER_API_KEY", "") or _os_cfg_get("llm_api_key", "")
# Anthropic SDK appends /v1/messages, so strip trailing /v1 from llm_base_url
_summarizer_base: str = os.environ.get("HAL_REALTIME_SUMMARIZER_BASE_URL", "") or _os_cfg_get("llm_base_url", "")
REALTIME_SUMMARIZER_BASE_URL: str = _summarizer_base.rstrip("/").removesuffix("/v1") if _summarizer_base else ""
REALTIME_SUMMARIZER_MODEL: str = os.environ.get("HAL_REALTIME_SUMMARIZER_MODEL", "claude-haiku-4-5-20251001")
# Extra attempts when a summarize call fails. The gateway drops requests
# intermittently — measured on lamp-0c89 03/09/2026: the SAME payload returned
# 404 once, then succeeded on four of the next five tries (the sixth timed out),
# while the superset payload containing it succeeded outright. So a failure says
# nothing about the input, and one dropped call costs the whole summary until
# the next rebuild. 0 disables retrying.
REALTIME_SUMMARIZER_RETRIES: int = int(
    os.environ.get("HAL_REALTIME_SUMMARIZER_RETRIES", "2")
)
# Seconds before the first retry; doubled for each one after it.
REALTIME_SUMMARIZER_RETRY_BACKOFF_S: float = float(
    os.environ.get("HAL_REALTIME_SUMMARIZER_RETRY_BACKOFF_S", "1.5")
)

# Gaze re-point: turn back toward the remembered bearing when nobody has been
# visible for a while.
#
# Needed because the idle recording is a LOOP of absolute poses that swings
# base_pitch about 17 degrees per cycle (see routes/emotion.py), so wherever the
# lamp is left, idle walks the camera back to the recording's own pose — which
# on a desk points at the keyboard, not at the user. Parking the remembered pose
# once would simply be overwritten by the next loop. Resting properly at the
# bearing would mean offsetting the whole playback by it, which is a change to
# motion playback rather than to this feature.
#
# So this does what a person does instead: if it cannot see who might be talking
# to it, it turns to where they usually are. Once, then it waits.
GAZE_REPOINT_ENABLED: bool = (
    os.environ.get("HAL_GAZE_REPOINT", "true").lower() in ("1", "true", "yes")
)
# Nobody WELL FRAMED for this long before turning. Long enough that leaning out
# of frame for a moment does not send the head hunting, short enough that the
# idle loop cannot walk the camera off the user and leave it there: the loop
# swings base_pitch every cycle, so the drift is continuous and a long timer
# simply means a long stretch where the feature cannot see who is talking.
GAZE_REPOINT_AFTER_S: float = float(
    os.environ.get("HAL_GAZE_REPOINT_AFTER_S", "12")
)
# ...and at most this often, so an empty desk does not become a lamp that turns
# every few seconds all night.
GAZE_REPOINT_COOLDOWN_S: float = float(
    os.environ.get("HAL_GAZE_REPOINT_COOLDOWN_S", "60")
)
# Do not turn to the remembered bearing while a face is THIS recently visible,
# however urgently the turn was asked for.
#
# The speech reacquire fires on "no usable face evidence", which means "I cannot
# tell whether they were facing me" — not "I cannot see them". Device-observed:
# `face=61px ... facing=0%/60% of 0 -> blind`, a face plainly in frame with the
# head turned away, and the lamp turned back to a bearing lower than the face
# the climb had just found. It threw away the framing it had, to go looking for
# the person it was already looking at.
GAZE_REPOINT_SKIP_IF_FACE_S: float = float(
    os.environ.get("HAL_GAZE_REPOINT_SKIP_IF_FACE_S", "3")
)
# How far off frame centre a face may sit and still count as "somebody is here,
# no need to turn". A face at the very edge is about to leave the frame, so
# treating it as well framed is what let the absence timer reset forever while
# the user drifted out of view — device-measured at edge=0.71-0.75 with the
# lamp still refusing to re-point.
GAZE_WELL_FRAMED_EDGE: float = float(
    os.environ.get("HAL_GAZE_WELL_FRAMED_EDGE", "0.6")
)
# Below this confidence the bearing is not worth moving for.
#
# 0.2, matching aim.MIN_BEARING_CONFIDENCE, so every consumer of the bearing
# trusts it at the same point. At 0.5 the watcher refused to turn to a bearing
# that look.aim and the search were both happily using — device-observed after a
# calibration change dropped the estimate: rebuilt to 0.38, the search seeded
# from it and the aim stepped toward it, while every reacquire logged
# "unavailable" and, because the sweep hangs off a repoint that has actually
# moved, the lamp never looked around either.
#
# A bearing good enough to aim a live conversational turn at is good enough to
# turn the head toward between them.
GAZE_REPOINT_MIN_CONFIDENCE: float = float(
    os.environ.get("HAL_GAZE_REPOINT_MIN_CONFIDENCE", "0.2")
)
# How long after turning to the remembered bearing to wait before deciding
# whether anyone was actually there.
#
# The estimate self-heals by being told when it predicted wrongly, and until now
# only a `look` ever told it — this watcher turned to the bearing and never
# reported back, so the consumer that acts MOST often was the one contributing
# nothing. Device-observed 2026-08-24: two repoints onto a confidence-0.99
# estimate, neither verified, the confidence unchanged afterwards.
#
# Long enough for the move to settle and the sampler to get several looks at the
# new view (it manages 3-6/s), short enough that the verdict still describes the
# moment the lamp turned.
GAZE_REPOINT_VERIFY_S: float = float(
    os.environ.get("HAL_GAZE_REPOINT_VERIFY_S", "6")
)

# Vertical centring — the neck the aim never had.
#
# The look aim drives yaw only, deliberately: the pitch sign was never validated
# for its relative `nudge()` path, and an inverted pitch is a bug this codebase
# already hit once. The consequence on a desk is that the camera cannot recover
# from pointing low, so the user's head sits above the frame and no amount of
# left-right correction brings it back.
#
# Device-measured which joint is actually the neck, by moving each and looking:
#   base_pitch   folds the whole arm, so it both rotates AND translates the
#                camera - not monotonic (10.6 raised the view, 18.6 lowered it)
#   elbow_pitch  0.2 px of vertical effect
#   wrist_pitch  -70.6 -> -55 -> -45 raised it steadily, bringing a face that
#                was clipped by the frame edge fully into view at 91 px
# So: wrist_pitch is the neck. The DIRECTION recorded above did not survive a
# paired re-measurement: holding the bus quiet and taking 18 face samples per
# position, interleaved, -75 -> -90 moved the face DOWN the frame (+0.103) and
# -68 -> -98 likewise (+0.185). Decreasing the joint tilts the camera UP. See
# _maybe_pitch in gaze.py, where the correction now carries that sign.
# Note the tracker's own pitch weights
# spread across base and elbow with PITCH_WEIGHT_WRIST = 0.0, which is likely
# why this joint was never the one anybody reached for.
# ON, after being off. The objection that turned it off was real and is worth
# keeping written down: camera direction depends on base_pitch and wrist_pitch
# TOGETHER — the arm folds at its base, so that joint rotates AND translates the
# camera, and the same wrist angle points somewhere different for every base
# angle. Driving one joint per-degree against a coupled two-link arm, with no
# kinematic model, walked the head to its limit while still missing the user.
#
# What changed is not the kinematics, it is that the corrections stopped being
# undone between steps. Two things were erasing them: the repoint re-anchoring
# idle on a remembered wrist angle recorded minutes earlier, and the idle loop
# pulling back toward the pose the recording was made at. With both fixed the
# loop converges instead of walking — device-measured, one run, three steps:
# 45% above centre -> 21% -> 16%, each starting exactly where the last left off.
#
# It is still open-loop against a coupled arm, so the limit case remains real:
# the same session drove wrist_pitch to -90.6 and sat on the mechanical stop
# before recovering. The per-step cap and the blind-step budget are what bound
# that, not any model of the arm. The remembered bearing (a full pose, restored
# in one absolute move — see bearing_sampler) is still the better mechanism when
# a pose has actually been learned; this loop is what gets a face into frame in
# the first place, so a pose worth remembering can be learned at all.
GAZE_PITCH_ENABLED: bool = (
    os.environ.get("HAL_GAZE_PITCH", "true").lower() in ("1", "true", "yes")
)
# Degrees of wrist_pitch per FULL frame height. A seed, not a calibration: the
# correction re-measures itself every step because it moves and then looks
# again, so an imperfect constant costs an extra iteration, not accuracy.
GAZE_PITCH_DEG_PER_FRAME: float = float(
    os.environ.get("HAL_GAZE_PITCH_DEG_PER_FRAME", "45")
)
# Largest single correction. Small enough that a wrong sign is a mistake the
# next measurement reverses, rather than a head swung to a limit.
#
# 15, not 8. The step is the binding constraint, not the estimate: a face 40%
# above centre asks for 18 degrees and got 8, so each correction fell short and
# the next frame reported almost the same offset — device-observed 41%, 33%,
# 42% across three corrections, converging on nothing. The sign is now measured
# rather than assumed, so the reason for keeping the step tiny is gone.
GAZE_PITCH_MAX_STEP_DEG: float = float(
    os.environ.get("HAL_GAZE_PITCH_MAX_STEP_DEG", "15")
)
# Vertical offset, as a fraction of frame height, that counts as centred enough.
# Wider than it needs to be: the aim is to get the face INSIDE the frame with
# room around it, not to centre it perfectly, and every correction is a visible
# head movement the user did not ask for.
GAZE_PITCH_DEAD_ZONE_FRAC: float = float(
    os.environ.get("HAL_GAZE_PITCH_DEAD_ZONE_FRAC", "0.15")
)
# Minimum gap between corrections, so a user who keeps moving does not turn the
# lamp into a head that nods along. Mostly superseded by GAZE_PITCH_WINDOW_S —
# the buffer is cleared after every correction, so refilling it already spaces
# them out — but kept as a floor.
GAZE_PITCH_COOLDOWN_S: float = float(
    os.environ.get("HAL_GAZE_PITCH_COOLDOWN_S", "4")
)
# How long a stretch of vertical offsets a correction is computed from.
#
# NOT the latest sample, which is what this loop used to do and why a validated
# sign still walked the head. `wrist_roll` is a second AIMING axis on this arm —
# device-proven 2026-08-24 by pinning every other joint and varying only roll:
# the horizon stayed level while the view panned, i.e. roll does not rotate the
# image, it points the camera. And idle.csv sweeps wrist_roll ~32 deg every ~10s
# cycle, forever.
#
# So the vertical offset a single frame reports is the framing error PLUS a
# periodic disturbance from wherever idle's roll happens to be. Measured on the
# same three frames, subject unmoved: dy +0.101 at roll -1.8 against +0.143 at
# roll +29.3 — 0.042 of frame height from roll alone, about 28% of the dead
# zone below, on a loop that fired every 4s from one sample.
#
# A median over a full idle cycle cancels a periodic disturbance while a real
# framing error survives it.
#
# 6, down from 12. The window is also what paces the loop: `_dy_estimate`
# refuses until the samples span WINDOW_S * 0.8, and the buffer is cleared after
# every correction, so the span requirement — not GAZE_PITCH_COOLDOWN_S — is the
# real gap between steps. At 12 that made a ~9.6s wait before the head would
# move at all, which is a long time to sit visibly badly framed while the user
# is right there. At 6 the same chain gives ~4.8s.
#
# The cost is real and worth stating: half an idle roll cycle, not a whole one.
# Roll is a second aiming axis and idle sweeps it every ~10s, so a half-period
# window carries some of that disturbance into the median instead of cancelling
# it — measured at ~0.042 of frame height, about 28% of the dead zone. Expect
# slightly noisier corrections that the next measurement walks back; the loop
# re-measures after every move, so an over-correction costs an extra iteration
# rather than accuracy. Put this back to 12 if the head starts hunting.
GAZE_PITCH_WINDOW_S: float = float(
    os.environ.get("HAL_GAZE_PITCH_WINDOW_S", "6")
)
# ...and this many measurements inside it, or the median is one noisy frame
# wearing a median's clothes. At GAZE_SAMPLE_FPS a full window holds far more;
# this is the floor for acting on a partly-filled one.
GAZE_PITCH_MIN_SAMPLES: int = int(
    os.environ.get("HAL_GAZE_PITCH_MIN_SAMPLES", "8")
)
# How few torso readings are enough when the climb has been asked for directly —
# a repoint that landed on a body and wants the head lifted now.
#
# Small because the torso path reports a CONSTANT -0.5, so more of them add no
# information. The full window still applies to face-driven corrections, where
# the offset actually varies and the median is doing real work.
GAZE_PITCH_PROMPT_MIN_SAMPLES: int = int(
    os.environ.get("HAL_GAZE_PITCH_PROMPT_MIN_SAMPLES", "2")
)
# Write an annotated frame beside every pitch correction, into
# SNAPSHOT_PERSIST_DIR/sensing_gaze/ (newest GAZE_SNAPSHOT_KEEP kept).
#
# The log line says the median was -0.41 of frame height. It cannot say whether
# that was the user's face, a colleague's, or a coat on a chair — and this loop
# spent a week walking the head while its numbers looked reasonable. Every time
# this feature was actually understood it was by looking at a picture: the
# clipped-eyes case (`landmarks_in_frame`), the wrong-person aim (F24), and the
# roll experiment that proved wrist_roll aims rather than rotates. The log is
# for noticing; the frame is for diagnosing.
#
# Same category convention and viewer as the bearing sampler's snapshots, so
# the two read identically.
GAZE_SNAPSHOT_ENABLED: bool = (
    os.environ.get("HAL_GAZE_SNAPSHOT", "true").lower() in ("1", "true", "yes")
)
GAZE_SNAPSHOT_KEEP: int = int(os.environ.get("HAL_GAZE_SNAPSHOT_KEEP", "40"))

# Check that a correction actually arrived, and stop asking a joint that cannot.
#
# `move_and_hold` reports nothing, so a stalled joint used to be indistinguish-
# able from a successful one: the loop re-commanded the same unreachable target
# every ~10s forever. Device-observed 2026-08-25 across six consecutive
# corrections, elbow_pitch read +12.3 every single time while being sent to
# +25.8 — only base_pitch's 10% share was landing, so the offset crept 43% ->
# 26% instead of closing.
#
# That is worse than slow. Re-commanding a stall is what heats a servo into
# giving up: the same elbow that stalled at +17.4 reached +44 three times out of
# three after 60s of rest. So the loop was manufacturing the condition it kept
# tripping over. Noticing the short move is what breaks that cycle.
GAZE_PITCH_LAND_TOL_DEG: float = float(
    os.environ.get("HAL_GAZE_PITCH_LAND_TOL_DEG", "2.0")
)
# When a repoint turns to the remembered bearing and finds nobody, look around
# before writing the user off.
#
# The cooldown is the whole safety of this. Gaze repoints whenever no face has
# been seen for GAZE_REPOINT_AFTER_S (12s), so with nothing holding it back a
# user out at lunch would get: 12s wait, turn, nobody, ~23s sweep, nobody, 12s
# wait, turn, nobody, sweep... a lamp scanning an empty room every thirty-five
# seconds for an hour. An absence should produce ONE search, not forty.
#
# Nobody asked for this sweep, which is the difference from the look-aim's: it
# is the same reasoning as GAZE_PITCH_COOLDOWN_S and the climb budget — an
# autonomous loop needs a bound on how often it may repeat itself.
GAZE_SWEEP_ENABLED: bool = (
    os.environ.get("HAL_GAZE_SWEEP", "true").lower() in ("1", "true", "yes")
)
GAZE_SWEEP_COOLDOWN_S: float = float(
    os.environ.get("HAL_GAZE_SWEEP_COOLDOWN_S", "900")
)
# How long nobody has to have been visible before the watcher looks around of
# its own accord.
#
# The sweep used to hang off a repoint that had MOVED and then missed, which
# made it unreachable in the two cases it is most wanted: no bearing to turn to,
# and already sitting on the bearing. Device-observed — the estimate was dropped
# by a calibration change, so every reacquire declined, and the lamp sat looking
# at nothing for as long as it was left.
#
# Longer than GAZE_REPOINT_AFTER_S (12s) so the cheap move is always tried first
# and the half-minute sweep stays the escalation, not the reflex. Note that is a
# different number from GAZE_REPOINT_COOLDOWN_S (60s), which is the gap BETWEEN
# repoints rather than the wait before one.
GAZE_SWEEP_AFTER_S: float = float(
    os.environ.get("HAL_GAZE_SWEEP_AFTER_S", "30")
)
# The cooldown when there is no bearing at all.
#
# Fifteen minutes is right for "I have a bearing, it missed, stop thrashing".
# It is wrong for "I have no idea where you are", because then the sweep is the
# ONLY way to find out and the lamp is forbidden from trying. Device-observed:
# three failed repoints dropped the estimate, and the lamp then sat unable to
# repoint (nothing to turn to) and unable to sweep (11 minutes left on the
# cooldown) while the user was talking to it.
GAZE_SWEEP_COOLDOWN_LOST_S: float = float(
    os.environ.get("HAL_GAZE_SWEEP_COOLDOWN_LOST_S", "120")
)

# --- climbing to find a face ------------------------------------------------
#
# A person box that TOUCHES the top edge of the frame means the body continues
# past it, so the head is above and this camera is aimed too low. That is the
# only shape of evidence used: an unclipped body with no face means the head IS
# in frame and simply was not detected — turned away, in profile, backlit — and
# climbing then aims at the ceiling for no reason.
#
# A fixed step rather than a proportional one, because the torso says "the head
# is up there somewhere" and never how far. Proportional control needs an error
# signal; this is a search.
GAZE_FACE_SEARCH_STEP_DEG: float = float(
    os.environ.get("HAL_GAZE_FACE_SEARCH_STEP_DEG", "15")
)
# About 60 degrees of climb. Bounded because the evidence stays true no matter
# how far the neck has already travelled, so acting on it forever is a loop and
# not a search.
GAZE_FACE_SEARCH_MAX_STEPS: int = int(
    os.environ.get("HAL_GAZE_FACE_SEARCH_MAX_STEPS", "4")
)

# Where the arm was standing the last time it could see a face. Separate from
# USER_BEARING_PATH deliberately — see face_height.py.
FACE_HEIGHT_PATH: str = os.environ.get(
    "HAL_FACE_HEIGHT_PATH", "/var/lib/hal/face_height.json"
)

# --- horizontal (pan) correction -------------------------------------------
#
# The mirror of the pitch knobs below, and deliberately LAZIER. Vertical framing
# fails in one direction — a user stands and leaves the top of frame — so it is
# worth chasing. Horizontal drift is mostly a person shifting in a chair, and a
# lamp that swings to follow every lean is the twitchy behaviour the whole gaze
# loop is damped to avoid. So: a wider dead zone and a smaller step than pitch.
GAZE_YAW_ENABLED: bool = (
    os.environ.get("HAL_GAZE_YAW", "true").lower() in ("1", "true", "yes")
)
GAZE_YAW_WINDOW_S: float = float(os.environ.get("HAL_GAZE_YAW_WINDOW_S", "12"))
GAZE_YAW_MIN_SAMPLES: int = int(os.environ.get("HAL_GAZE_YAW_MIN_SAMPLES", "8"))
# dx runs -0.5 (left edge) to +0.5 (right edge), so this band is a fraction of
# the FULL frame width: 0.10 lets the face sit a fifth of the way to the edge
# before the lamp turns.
#
# Started at 0.22 on the reasoning that horizontal drift is just someone
# shifting in a chair and worth ignoring. Device-observed: deliberately moving
# side to side at a desk peaked at dx=+20%, so the loop measured the movement
# correctly and declined every time — a threshold that wide barely fires.
#
# Narrower than the pitch dead zone (0.15) rather than wider, because the value
# tested is the MEDIAN over GAZE_YAW_WINDOW_S. The window is what rejects
# leaning and fidgeting; making the dead zone do that job a second time only
# costs the correction it was supposed to allow.
GAZE_YAW_DEAD_ZONE_FRAC: float = float(
    os.environ.get("HAL_GAZE_YAW_DEAD_ZONE_FRAC", "0.10")
)
# dx is a fraction of frame WIDTH, and the lens spans far more degrees
# horizontally than the correction needs to be aggressive about.
GAZE_YAW_DEG_PER_FRAME: float = float(
    os.environ.get("HAL_GAZE_YAW_DEG_PER_FRAME", "40")
)
GAZE_YAW_MAX_STEP_DEG: float = float(
    os.environ.get("HAL_GAZE_YAW_MAX_STEP_DEG", "12")
)
# Neither pan joint fights gravity, so they arrive quickly — but the whole lamp
# turning is a bigger visual event than a head tilt, so it gets the same gentle
# second as the pitch correction rather than the brisk look.aim quarter.
GAZE_YAW_MOVE_S: float = float(os.environ.get("HAL_GAZE_YAW_MOVE_S", "1.0"))

# How long a pitch correction takes, in seconds.
#
# Separate from aim.MOVE_DURATION_S (0.25) on purpose. That constant is shared
# with look.aim, which runs up to six iterations per call and wants each one
# brisk; stretching it there would turn a ~3.5s "look at me" into ~8s. Gaze
# makes ONE unrequested move every ten seconds or so, and there is nothing
# waiting on it, so it can afford to be gentle.
#
# 0.25s put a full 15 deg correction at 60 deg/s — inside the SAFETY.md ceiling
# of 120, but a visible snap on a desk lamp and needlessly hard on a joint that
# spends the move fighting gravity. At 1.0s the same correction runs at 15 deg/s.
GAZE_PITCH_MOVE_S: float = float(
    os.environ.get("HAL_GAZE_PITCH_MOVE_S", "1.0")
)
# Let the arm arrive before reading it back — a read taken mid-glide reports a
# short move that is merely still moving.
GAZE_PITCH_SETTLE_S: float = float(
    os.environ.get("HAL_GAZE_PITCH_SETTLE_S", "1.8")
)
# How long a joint that came up short is left out of the allocation. Matched to
# the measured recovery: a rested elbow was reliable again after ~60s.
GAZE_PITCH_STALL_REST_S: float = float(
    os.environ.get("HAL_GAZE_PITCH_STALL_REST_S", "60")
)
# Stop short of where it actually stalled, so the retry does not lean on the
# stop again the moment the joint is readmitted.
GAZE_PITCH_STALL_BACKOFF_DEG: float = float(
    os.environ.get("HAL_GAZE_PITCH_STALL_BACKOFF_DEG", "2.0")
)
