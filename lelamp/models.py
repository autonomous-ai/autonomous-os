"""
LeLamp Pydantic request/response models.

All FastAPI endpoint models live here — import from server.py via `from lelamp.models import *`.
"""

from typing import Optional, Union

from pydantic import BaseModel, Field

from lelamp.service.voice.tts_backend import PROVIDER_OPENAI, PROVIDER_ELEVENLABS


class ServoRequest(BaseModel):
    recording: str

    model_config = {"json_schema_extra": {"examples": [{"recording": "curious"}]}}


class ServoStateResponse(BaseModel):
    available_recordings: list[str]
    current: Optional[str]

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "available_recordings": [
                        "nod",
                        "curious",
                        "happy_wiggle",
                        "idle",
                        "sad",
                        "excited",
                        "shy",
                        "shock",
                        "listening",
                        "thinking_deep",
                        "laugh",
                        "confused",
                        "sleepy",
                        "greeting",
                        "goodbye",
                        "acknowledge",
                        "stretching",
                        "scanning",
                        "wake_up",
                        "headshake",
                        "music_groove",
                        "music_chill",
                        "music_hype",
                    ],
                    "current": "idle",
                }
            ]
        }
    }


class LEDSolidRequest(BaseModel):
    color: Union[list[int], int]
    transient: bool = Field(
        False,
        description="If true, don't overwrite user LED state (used by Buddy/transient overlays).",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"color": [255, 100, 0]},
                {"color": 16711680},
            ]
        }
    }


class LEDOffRequest(BaseModel):
    transient: bool = False


class LEDPaintRequest(BaseModel):
    colors: list[Union[list[int], int]]

    model_config = {
        "json_schema_extra": {
            "examples": [{"colors": [[255, 0, 0], [0, 255, 0], [0, 0, 255]]}]
        }
    }


class LEDStateResponse(BaseModel):
    led_count: int


class LEDColorResponse(BaseModel):
    led_count: int
    on: bool  # True if any pixel is lit
    color: list[int]  # [R, G, B] — actual pixel 0 from strip
    hex: str  # e.g. "#ff8800"
    brightness: float  # 0.0–1.0 derived from max channel
    effect: Optional[str]  # running effect name, or null
    scene: Optional[str]  # active scene name, or null


class LEDEffectRequest(BaseModel):
    effect: str = Field(
        ...,
        description="Effect name: breathing, candle, rainbow, notification_flash, pulse, blink",
    )
    color: Optional[list[int]] = Field(
        None, description="Base RGB color for the effect (default: current color)"
    )
    speed: float = Field(
        1.0,
        ge=0.1,
        le=5.0,
        description="Speed multiplier (0.1=slow, 1.0=normal, 5.0=fast)",
    )
    duration_ms: Optional[int] = Field(
        None, ge=100, le=60000, description="Auto-stop after duration (null=indefinite)"
    )
    transient: bool = Field(
        False,
        description="If true, don't overwrite user LED state (used by Buddy/transient overlays).",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"effect": "breathing", "color": [255, 100, 0], "speed": 1.0},
                {"effect": "rainbow", "speed": 0.5},
                {
                    "effect": "notification_flash",
                    "color": [255, 0, 0],
                    "duration_ms": 3000,
                },
            ]
        }
    }


class LEDEffectResponse(BaseModel):
    status: str
    effect: str
    speed: float


class StatusResponse(BaseModel):
    status: str


class VolumeRequest(BaseModel):
    volume: int = Field(..., ge=0, le=100, description="Volume percentage 0-100")

    model_config = {"json_schema_extra": {"examples": [{"volume": 75}]}}


class AudioDevicesResponse(BaseModel):
    output_device: Optional[int]
    input_device: Optional[int]
    available: bool


class CameraInfoResponse(BaseModel):
    available: bool
    # Actual capture mode the device negotiated (None until the capture loop
    # has opened the device once). Falls back to configured CAMERA_WIDTH/
    # CAMERA_HEIGHT when device has not reported yet.
    width: Optional[int]
    height: Optional[int]
    fps: Optional[float] = None
    disabled: bool = False
    manual_override: bool = False
    zoom: float = 1.0


class CameraZoomRequest(BaseModel):
    zoom: float = Field(..., ge=1.0, le=5.0, description="Digital zoom factor, 1.0 = no zoom")

    model_config = {"json_schema_extra": {"examples": [{"zoom": 2.0}]}}


class EmotionRequest(BaseModel):
    emotion: str = Field(
        ...,
        description="Emotion name: curious, happy, sad, thinking, idle, excited, shy, shock",
    )
    intensity: float = Field(0.7, ge=0.0, le=1.0, description="Intensity 0.0-1.0")

    model_config = {
        "json_schema_extra": {"examples": [{"emotion": "curious", "intensity": 0.8}]}
    }


class EmotionResponse(BaseModel):
    status: str
    emotion: str
    servo: Optional[str]
    led: Optional[list[int]]


class SceneRequest(BaseModel):
    scene: str = Field(
        ..., description="Scene name: reading, focus, relax, movie, night, energize"
    )

    model_config = {"json_schema_extra": {"examples": [{"scene": "reading"}]}}


class SceneResponse(BaseModel):
    status: str
    scene: str
    brightness: float
    color: list[int]


class SpeakRequest(BaseModel):
    text: str = Field(
        ..., min_length=1, max_length=2000, description="Text to speak via TTS"
    )
    voice: str = Field("", description="Override TTS voice for this request (e.g. 'Rachel', 'Brian')")
    # When True, this speech can be interrupted by the next speak() call (e.g. dead air filler).
    interruptible: bool = Field(False, description="If True, can be interrupted by next speech")
    # Optional provider override for one-off tests (e.g. web TTS preview before saving config).
    # When set and differs from the running service, the backend is hot-swapped using the
    # supplied credentials so the test does not require restarting /voice/start.
    provider: Optional[str] = Field(None, description="Override TTS provider: 'openai' or 'elevenlabs'")
    tts_api_key: Optional[str] = Field(None, description="API key for provider override")
    tts_base_url: Optional[str] = Field(None, description="Base URL for provider override")
    # Cache controls — see tts_service.speak_cached(). Cache key includes
    # provider/voice/model/speed/text so config changes invalidate naturally.
    cached: bool = Field(False, description="Look up WAV cache; render+save on miss")
    prerender: bool = Field(False, description="Render+save to cache without playing (warmup)")

    model_config = {
        "json_schema_extra": {"examples": [{"text": "[laugh] Hey! How are you doing today? I missed you! [sigh] It has been so quiet around here.", "voice": "Rachel"}]}
    }


class MusicPlayRequest(BaseModel):
    query: str = Field(
        ..., min_length=1, max_length=500, description="Song name or search query"
    )
    person: str = Field(
        default="", max_length=64, description="Person name (from face recognition) for per-user history"
    )

    model_config = {
        "json_schema_extra": {"examples": [{"query": "The Calling - Wherever You Will Go", "person": "alice"}]}
    }


class MusicStatusResponse(BaseModel):
    available: bool
    playing: bool
    title: Optional[str] = None
    speaker_muted: bool = False


class VolumeResponse(BaseModel):
    control: str
    volume: int


class ServoPositionResponse(BaseModel):
    positions: dict[str, float]


class ServoDetail(BaseModel):
    id: int
    angle: Optional[float]
    online: bool
    error: Optional[str] = None


class ServoStatusResponse(BaseModel):
    servos: dict[str, ServoDetail]


class ServoAimRequest(BaseModel):
    direction: str = Field(
        ...,
        description="Named direction: desk, wall, left, right, up, down, center, user",
    )
    duration: float = Field(
        2.0, ge=0.0, le=10.0, description="Move duration in seconds (default: 2.0)"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [{"direction": "desk"}, {"direction": "left", "duration": 3.0}]
        }
    }


class ServoNudgeRequest(BaseModel):
    yaw: float = Field(0.0, ge=-180.0, le=180.0, description="Relative yaw in degrees (negative=left, positive=right)")
    pitch: float = Field(0.0, ge=-90.0, le=90.0, description="Relative pitch in degrees (negative=down, positive=up)")
    duration: float = Field(2.0, ge=0.0, le=10.0, description="Move duration in seconds")

    model_config = {
        "json_schema_extra": {
            "examples": [{"yaw": -15}, {"yaw": 30, "pitch": 10, "duration": 1.5}]
        }
    }


class ServoAimResponse(BaseModel):
    status: str
    direction: str
    positions: dict[str, float]


class SceneListResponse(BaseModel):
    scenes: list[str]
    active: Optional[str]  # currently active scene name, or null


class PresenceResponse(BaseModel):
    state: str
    enabled: bool
    seconds_since_motion: int
    idle_timeout: int
    away_timeout: int


class FaceEnrollRequest(BaseModel):
    image_base64: str = Field(..., description="Base64-encoded image (JPEG or PNG)")
    label: str = Field(..., min_length=1, max_length=64, description="Person name")
    telegram_username: Optional[str] = Field(None, description="Telegram username of the person")
    telegram_id: Optional[str] = Field(None, description="Telegram user ID for DM targeting")


class FaceEnrollResponse(BaseModel):
    status: str
    label: str
    telegram_username: Optional[str] = None
    telegram_id: Optional[str] = None
    photo_path: str
    enrolled_count: int


class FaceStatusResponse(BaseModel):
    enrolled_count: int
    enrolled_names: list[str]


class FacePersonDetail(BaseModel):
    label: str
    telegram_username: Optional[str] = None
    telegram_id: Optional[str] = None
    photo_count: int
    photos: list[str]  # filenames, e.g. ["1711929600000.jpg"]
    mood_days: list[str] = []  # e.g. ["2026-04-09"]
    wellbeing_days: list[str] = []  # e.g. ["2026-04-10"]
    music_suggestion_days: list[str] = []  # e.g. ["2026-04-17"]
    posture_days: list[str] = []  # e.g. ["2026-05-14"] — RULA ergo alerts + nudges
    audio_history_days: list[str] = []  # e.g. ["2026-04-17"]
    voice_samples: list[str] = []  # files in voice/ — wav samples + metadata.json
    habit_patterns: bool = False  # True if habit/patterns.json exists
    files: list[str] = []  # all non-photo files


class FaceOwnersDetailResponse(BaseModel):
    enrolled_count: int
    persons: list[FacePersonDetail]


class UserInfoResponse(BaseModel):
    name: str
    is_friend: bool
    telegram_id: Optional[str] = None
    telegram_username: Optional[str] = None


class FaceRemoveRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=64)


class FacePhotoRemoveRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=64)
    filename: str = Field(..., min_length=1, max_length=128)


class FaceRemoveResponse(BaseModel):
    status: str
    label: str
    enrolled_count: int


class FaceResetResponse(BaseModel):
    status: str
    enrolled_count: int


class SensingResponse(BaseModel):
    running: bool
    poll_interval: float
    last_event_seconds_ago: dict[str, int]
    perceptions: list[dict]
    presence: dict


class DisplayStateResponse(BaseModel):
    mode: str
    hardware: bool
    available_expressions: list[str]


class VoiceStatusResponse(BaseModel):
    voice_available: bool
    voice_listening: bool
    tts_available: bool
    tts_speaking: bool
    tts_detail: Optional[dict] = None
    mic_muted: bool = False


class HealthResponse(BaseModel):
    status: str
    servo: bool
    led: bool
    camera: bool
    audio: bool
    sensing: bool
    voice: bool
    tts: bool
    music: bool
    display: bool


class ServoMoveRequest(BaseModel):
    positions: dict[str, float] = Field(
        ...,
        description=(
            "Joint positions (degrees). Ordered by servo ID: "
            "base_yaw.pos (ID 1, min -90 max 90), "
            "base_pitch.pos (ID 2, min -90 max 90), "
            "elbow_pitch.pos (ID 3, min -90 max 90), "
            "wrist_roll.pos (ID 4, min -90 max 90), "
            "wrist_pitch.pos (ID 5, min -90 max 90). "
            "Values are clamped to safe limits automatically."
        ),
    )
    duration: float = Field(
        2.0,
        ge=0.0,
        le=10.0,
        description="Move duration in seconds. 0 = instant jump, >0 = smooth interpolation (default: 2.0)",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "positions": {
                        "base_yaw.pos": 0.0,
                        "base_pitch.pos": 10.0,
                        "elbow_pitch.pos": -5.0,
                        "wrist_roll.pos": 0.0,
                        "wrist_pitch.pos": 0.0,
                    },
                    "_comment": "ID1 base_yaw [-90,90] | ID2 base_pitch [-90,90] | ID3 elbow_pitch [-90,90] | ID4 wrist_roll [-90,90] | ID5 wrist_pitch [-90,90]",
                },
                {
                    "positions": {"base_pitch.pos": 5.0, "elbow_pitch.pos": 5.0},
                    "duration": 3.0,
                },
            ]
        }
    }


class ServoMoveResponse(BaseModel):
    status: str
    requested: dict[str, float]
    clamped: dict[str, float]  # kept for API compat, same as requested
    duration: float
    errors: Optional[dict[str, str]] = None


class ServoTrackRequest(BaseModel):
    bbox: Optional[list[int]] = Field(
        None, min_length=4, max_length=4,
        description="Bounding box [x, y, w, h]. If omitted, auto-detect using YOLOWorld.",
    )
    target: Union[str, list[str]] = Field(
        "",
        description=(
            "Object name(s) to track. Pass a single string (e.g. 'cup') or a list "
            "of candidate labels (e.g. ['cup', 'mug', 'coffee cup']) when the caller "
            "is unsure of the exact word — YOLOWorld evaluates all candidates and "
            "the highest-confidence detection is used."
        ),
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"target": "cup"},
                {"target": ["cup", "mug", "coffee cup"]},
                {"bbox": [280, 200, 80, 80], "target": "water bottle"},
            ]
        }
    }


class ServoTrackResponse(BaseModel):
    status: str
    tracking: bool
    target: Optional[str] = None
    bbox: Optional[list[int]] = None
    confidence: Optional[float] = None


class DisplayEyesRequest(BaseModel):
    expression: str = Field(
        ...,
        description="Expression: neutral, happy, sad, curious, thinking, excited, shy, shock, sleepy, angry, love",
    )
    pupil_x: float = Field(
        0.0, ge=-1.0, le=1.0, description="Pupil X: -1.0 (left) to 1.0 (right)"
    )
    pupil_y: float = Field(
        0.0, ge=-1.0, le=1.0, description="Pupil Y: -1.0 (up) to 1.0 (down)"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [{"expression": "happy", "pupil_x": 0.0, "pupil_y": 0.0}]
        }
    }


class DisplayInfoRequest(BaseModel):
    text: str = Field(
        ..., min_length=1, max_length=20, description="Main text (short, e.g. '14:30')"
    )
    subtitle: str = Field(
        "", max_length=40, description="Subtitle (e.g. 'Good afternoon')"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [{"text": "14:30", "subtitle": "Good afternoon"}]
        }
    }


class VoiceStartRequest(BaseModel):
    llm_api_key: str = Field(
        ..., min_length=1, description="OpenAI-compatible API key for AutonomousSTT (LLM-as-STT fallback)"
    )
    llm_base_url: str = Field(
        ..., min_length=1, description="OpenAI-compatible base URL for TTS and STT"
    )
    tts_api_key: str = Field(
        "",
        description="TTS provider API key (OpenAI / ElevenLabs). Empty falls back to llm_api_key — for households where TTS and LLM share one credential.",
    )
    stt_api_key: str = Field(
        "",
        description="AutonomousSTT API key. Empty falls back to llm_api_key. Ignored when deepgram_api_key is set.",
    )
    stt_base_url: str = Field(
        "", description="AutonomousSTT base URL. Empty falls back to llm_base_url.",
    )
    tts_base_url: str = Field(
        "", description="TTS provider base URL. Empty falls back to llm_base_url.",
    )
    deepgram_api_key: str = Field(
        "", description="Deepgram API key (optional, falls back to Autonomous STT)"
    )
    tts_voice: str = Field(
        "", description="TTS voice name (optional, defaults to config TTS_VOICE)"
    )
    tts_instructions: str = Field(
        "", description="TTS style/vibe instructions (optional, e.g. 'Speak warmly')"
    )
    tts_provider: str = Field(
        PROVIDER_OPENAI, description=f"TTS provider: '{PROVIDER_OPENAI}' (default) or '{PROVIDER_ELEVENLABS}'"
    )


class VoiceConfigRequest(BaseModel):
    wake_words: list[str] = Field(..., min_length=1, description="Wake word list (lowercase matched)")
