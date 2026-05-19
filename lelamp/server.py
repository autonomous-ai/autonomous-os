"""
LeLamp Hardware Runtime -- FastAPI server on port 5001.

Only starts the drivers we need. LiveKit/OpenAI code stays untouched but never imported.
Lumi Server (Go, port 5000) bridges requests here.
"""

import json
import logging
import logging.handlers
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
# Load .env BEFORE any lelamp imports so config.py reads correct env vars
load_dotenv(Path(__file__).parent / ".env", override=False)

from fastapi import FastAPI

import lelamp.app_state as state
from lelamp.config import (
    AUDIO_INPUT_ALSA,
    AUDIO_OUTPUT_ALSA,
    AUDIO_SENSING_DEVICE,
    CAMERA_HEIGHT,
    CAMERA_INDEX,
    CAMERA_WIDTH,
    HTTP_HOST,
    HTTP_PORT,
    LAMP_ID,
    MODE,
    SERVO_FPS,
    SERVO_HOLD_S,
    SERVO_PORT,
    TTS_SPEED,
    TTS_VOICE,
    TTS_INSTRUCTIONS,
    LUMI_CONFIG_PATH,
)
from lelamp.models import HealthResponse, StatusResponse
from lelamp.presets import SCENE_PRESETS, SERVO_CMD_PLAY

# --- Logging: colored stdout + rotating file ---
LOG_DIR = Path(os.environ.get("LELAMP_LOG_DIR", "/var/log/lelamp"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

_LEVEL_COLORS = {
    logging.DEBUG: "\033[37m",     # gray
    logging.INFO: "\033[32m",      # green
    logging.WARNING: "\033[33m",   # yellow
    logging.ERROR: "\033[31m",     # red
    logging.CRITICAL: "\033[1;31m",  # bold red
}
_RESET = "\033[0m"


class _ColorFormatter(logging.Formatter):
    """Adds ANSI colors to levelname for console output."""

    _fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"

    def format(self, record):
        color = _LEVEL_COLORS.get(record.levelno, "")
        record.levelname = f"{color}{record.levelname}{_RESET}"
        formatter = logging.Formatter(self._fmt)
        return formatter.format(record)


_root = logging.getLogger()
_log_level = os.environ.get("LELAMP_LOG_LEVEL", "INFO").upper()
_root.setLevel(getattr(logging, _log_level, logging.INFO))

# Console handler (colored)
_console = logging.StreamHandler()
_console.setFormatter(_ColorFormatter())
_root.addHandler(_console)

# File handler: 1 MB per file, keep 3 backups (~4 MB max) -- no color codes
_file = logging.handlers.RotatingFileHandler(
    LOG_DIR / "server.log",
    maxBytes=1 * 1024 * 1024,
    backupCount=3,
)
_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
_root.addHandler(_file)


# GELF handler: send INFO+ logs to centralized Graylog
try:
    from lelamp.service.gelf_handler import GELFHandler
    from lelamp.config import _lumi_cfg_get

    _gelf = GELFHandler()
    _gelf.setFormatter(logging.Formatter("%(message)s"))
    _device_id = _lumi_cfg_get("device_id")
    if _device_id:
        _gelf.set_host(_device_id)
    _root.addHandler(_gelf)
except Exception:
    pass

logger = logging.getLogger("lelamp.server")
logger.info("Logging to %s/server.log", LOG_DIR)

# --- Lazy imports for hardware drivers (may not be available on dev machines) ---

AnimationService = None
RGBService = None
sd = None
np = None

try:
    from lelamp.service.motors.animation_service import AnimationService
except ImportError as e:
    logger.warning(f"Servo drivers not available: {e}")

try:
    from lelamp.service.rgb.rgb_service import RGBService
except ImportError as e:
    logger.warning(f"LED drivers not available: {e}")

try:
    import numpy as np
    import sounddevice as sd
except ImportError as e:
    logger.warning(f"Audio drivers not available: {e}")

cv2 = None
try:
    import cv2
except ImportError as e:
    logger.warning(f"Camera drivers (opencv) not available: {e}")

LocalVideoCaptureDevice = None
VideoCaptureDeviceInfo = None
try:
    from lelamp.devices.models import VideoCaptureDeviceInfo
    from lelamp.devices.video_capture_device import LocalVideoCaptureDevice
except ImportError as e:
    logger.warning(f"Video capture device not available: {e}")

SensingService = None
FacePerception = None
try:
    from lelamp.service.sensing.perceptions.processors.facerecognizer import FacePerception
    from lelamp.service.sensing.sensing_service import SensingService
except ImportError as e:
    logger.warning(f"Sensing service not available: {e}")
    SensingService = None
    FacePerception = None

VoiceService = None
DeepgramSTT = None
AutonomousSTT = None
TTSService = None
try:
    from lelamp.service.voice.stt_autonomous import AutonomousSTT
    from lelamp.service.voice.stt_deepgram import DeepgramSTT
    from lelamp.service.voice.voice_service import VoiceService
except ImportError as e:
    logger.warning(f"Voice service not available: {e}")

try:
    from lelamp.service.voice.tts_service import TTSService
    from lelamp.service.voice.tts_backend import PROVIDER_OPENAI
except ImportError as e:
    logger.warning(f"TTS service not available: {e}")

MusicService = None
try:
    from lelamp.service.voice.music_service import MusicService
except ImportError as e:
    logger.warning(f"Music service not available: {e}")

DisplayService = None
try:
    from lelamp.service.display.display_service import DisplayService
except ImportError as e:
    logger.warning(f"Display service not available: {e}")

_gpio_button_handler = None
_ttp223_handler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _gpio_button_handler, _ttp223_handler

    # --- Phase 1: Fire slow hardware init in background threads ---

    def _init_servo():
        if not AnimationService:
            return
        try:
            svc = AnimationService(
                port=SERVO_PORT, lamp_id=LAMP_ID, fps=SERVO_FPS, hold_s=SERVO_HOLD_S
            )
            svc.start()
            state.animation_service = svc
            logger.info("AnimationService started")
        except Exception as e:
            logger.warning(f"AnimationService failed to start: {e}")

    def _init_led():
        if not RGBService:
            return
        try:
            svc = RGBService(led_count=64)
            svc.start()
            state.rgb_service = svc
            logger.info("RGBService started")
        except Exception as e:
            logger.warning(f"RGBService failed to start: {e}")

    def _init_camera():
        if not (LocalVideoCaptureDevice and VideoCaptureDeviceInfo and cv2):
            return
        try:
            cap = LocalVideoCaptureDevice(
                VideoCaptureDeviceInfo(
                    device_id=CAMERA_INDEX,
                    max_width=CAMERA_WIDTH,
                    max_height=CAMERA_HEIGHT,
                )
            )
            cap.start()
            state.camera_capture = cap
            logger.info(
                f"Camera opened (index={CAMERA_INDEX}, {CAMERA_WIDTH}x{CAMERA_HEIGHT})"
            )
        except Exception as e:
            logger.warning(f"Camera failed to start: {e}")

    hw_threads = []
    for fn in (_init_servo, _init_led, _init_camera):
        t = threading.Thread(target=fn, daemon=True, name=fn.__name__)
        t.start()
        hw_threads.append(t)

    # --- Phase 2: Audio detect + TTS + VoiceService ---

    if sd:
        _audio_results = [None, None]

        def _detect_output():
            _audio_results[0] = state._find_audio_device(output=True)

        def _detect_input():
            _audio_results[1] = state._find_audio_device(output=False)

        _t_out = threading.Thread(target=_detect_output, daemon=True)
        _t_in = threading.Thread(target=_detect_input, daemon=True)
        _t_out.start()
        _t_in.start()
        _t_out.join()
        _t_in.join()

        state.audio_output_device, state.audio_input_device = _audio_results
        _out_env = os.environ.get("LELAMP_AUDIO_OUTPUT_DEVICE")
        if _out_env is not None:
            state.audio_output_device = int(_out_env)
            logger.info("Audio output device override from env: %d", state.audio_output_device)
        elif os.environ.get("LELAMP_AUDIO_OUTPUT_ALSA"):
            _alsa_out = os.environ["LELAMP_AUDIO_OUTPUT_ALSA"]
            _alsa_card = _alsa_out.split(":")[1].split(",")[0] if ":" in _alsa_out else ""
            if _alsa_card:
                # ALSA short card id (e.g. "wm8960soundcard") and PortAudio device
                # label (e.g. "wm8960-soundcard: ...") often differ by dashes/
                # underscores. Normalize both sides so matching is robust.
                def _norm(s: str) -> str:
                    return "".join(c for c in s.lower() if c.isalnum())
                _needle = _norm(_alsa_card)
                # PortAudio caches its device list at sd import time. At OS cold
                # boot, sndi2s4 (ES8389 codec) often isn't registered yet, so the
                # cached enum lacks both the hw card and any asound.conf plug
                # alias that points at it (e.g. plug:lamp_speaker). Force a
                # fresh enum each retry via _terminate+_initialize, until the
                # alias appears or we time out (~10s).
                _matched = False
                for _attempt in range(20):
                    for _i, _d in enumerate(sd.query_devices()):
                        if _needle in _norm(_d["name"]) and _d["max_output_channels"] > 0:
                            state.audio_output_device = _i
                            logger.info(
                                "Audio output device from ALSA env: %d '%s' (matched '%s', attempt=%d)",
                                _i, _d["name"], _alsa_card, _attempt + 1,
                            )
                            _matched = True
                            break
                    if _matched:
                        break
                    try:
                        sd._terminate()
                        sd._initialize()
                    except Exception:
                        logger.exception("sounddevice reinit failed")
                    time.sleep(0.5)
                if not _matched:
                    logger.warning(
                        "ALSA env '%s' never enumerated by PortAudio after 10s; "
                        "TTS will use _find_audio_device fallback (likely silent)",
                        _alsa_out,
                    )
        if state.audio_output_device is not None:
            logger.info(f"Audio output device: {state.audio_output_device}")
        if state.audio_input_device is not None:
            logger.info(f"Audio input device: {state.audio_input_device}")

    # Auto-start voice pipeline from Lumi config
    lumi_config_path = LUMI_CONFIG_PATH
    try:
        with open(lumi_config_path) as f:
            lumi_cfg = json.load(f)
        dgk = lumi_cfg.get("deepgram_api_key", "")
        llm_key = lumi_cfg.get("llm_api_key", "")
        llm_url = lumi_cfg.get("llm_base_url", "")
        voice = lumi_cfg.get("tts_voice", "") or TTS_VOICE
        tts_provider = lumi_cfg.get("tts_provider", PROVIDER_OPENAI)
        if llm_key and llm_url and TTSService and not state.tts_service:
            state.tts_service = TTSService(
                api_key=llm_key,
                base_url=llm_url,
                sound_device_module=sd,
                numpy_module=np,
                output_device=state.audio_output_device,
                voice=voice,
                speed=TTS_SPEED,
                instructions=lumi_cfg.get("tts_instructions", "") or TTS_INSTRUCTIONS or None,
                on_speak_start=state._on_tts_speak_start,
                on_speak_end=state._on_tts_speak_end,
                provider=tts_provider,
            )
            logger.info(
                "TTSService auto-started (provider=%s, output_device=%s, available=%s)",
                tts_provider,
                state.audio_output_device,
                state.tts_service.available,
            )
        if VoiceService and not state.voice_service:
            agent_name = state._read_agent_name(lumi_cfg)
            wake_words = state._build_wake_words(agent_name)
            stt_provider = None
            logger.info("STT selection: deepgram_key=%s, DeepgramSTT=%s, AutonomousSTT=%s, agent=%s",
                        bool(dgk), DeepgramSTT is not None, AutonomousSTT is not None, agent_name)
            if dgk and DeepgramSTT:
                dg_keywords = [f"{agent_name}:3"]
                if " " in agent_name:
                    dg_keywords.append(" ".join(agent_name) + ":2")
                stt_provider = DeepgramSTT(api_key=dgk, keywords=dg_keywords)
            elif llm_key and llm_url and AutonomousSTT:
                stt_model = (lumi_cfg.get("stt_model") or "").strip() or None
                stt_language = (lumi_cfg.get("stt_language") or "").strip() or None
                stt_kwargs = {}
                if stt_model:
                    stt_kwargs["model"] = stt_model
                if stt_language:
                    stt_kwargs["language"] = stt_language
                stt_keywords = [f"{agent_name}:3"]
                if " " in agent_name:
                    stt_keywords.append(" ".join(agent_name) + ":2")
                stt_provider = AutonomousSTT(
                    api_key=llm_key, base_url=llm_url,
                    keywords=stt_keywords, **stt_kwargs
                )
            if stt_provider:
                state.voice_service = VoiceService(
                    stt_provider=stt_provider,
                    input_device=state.audio_input_device,
                    tts_service=state.tts_service,
                    music_service=state.music_service,
                    wake_words=wake_words,
                    alsa_device=AUDIO_INPUT_ALSA,
                )
                state.voice_service.start()
                logger.info("VoiceService auto-started (%s, wake_words=%s)", stt_provider.name, wake_words)
    except FileNotFoundError:
        logger.info(
            f"Lumi config not found at {lumi_config_path}, voice will wait for /voice/start"
        )
    except Exception as e:
        logger.warning(f"Auto-start voice from lumi config failed: {e}")

    # Start music service
    if MusicService:
        try:
            from lelamp.routes.music import _on_music_complete

            state.music_service = MusicService(on_complete=_on_music_complete)
            if state.tts_service:
                state.music_service._tts_service = state.tts_service
            if state.voice_service:
                state.voice_service.set_music_service(state.music_service)
            logger.info("MusicService started")
        except Exception as e:
            logger.warning(f"MusicService failed to start: {e}")

    # Pre-render music backchannel cues so audio_play hits the cache (~50ms)
    # instead of paying a TTS round-trip on the first play. Runs in a daemon
    # thread so a slow first render doesn't delay startup.
    def _prerender_music_backchannel():
        if not state.tts_service or not getattr(state.tts_service, "available", False):
            return
        try:
            from lelamp.routes.music import _backchannel_pool
            for phrase in _backchannel_pool():
                state.tts_service.speak_cached(phrase, prerender=True)
        except Exception as e:
            logger.warning("Music backchannel prerender failed: %s", e)

    threading.Thread(
        target=_prerender_music_backchannel,
        daemon=True,
        name="prerender-music-backchannel",
    ).start()

    # --- Phase 3: Wait for hardware threads, then start hardware-dependent services ---
    for t in hw_threads:
        t.join(timeout=10)

    # Start sensing loop
    sensing_enabled = os.environ.get("LELAMP_SENSING_ENABLED", "true").lower() in (
        "true",
        "1",
        "yes",
    )
    if SensingService and sensing_enabled:
        try:
            from lelamp.routes.servo import aim_servo
            from lelamp.models import ServoAimRequest

            def _presence_restore_aim():
                """Re-aim lamp to active scene direction when presence restores light."""
                if not state._active_scene:
                    logger.info("Presence aim restore: no active scene -- skipping aim")
                    return
                if not state.animation_service:
                    logger.warning("Presence aim restore: animation_service not available")
                    return
                preset = SCENE_PRESETS.get(state._active_scene)
                aim_dir = preset.get("aim") if preset else None
                if aim_dir:
                    logger.info("Presence aim restore: scene=%s aim=%s", state._active_scene, aim_dir)
                    threading.Thread(
                        target=aim_servo,
                        args=(ServoAimRequest(direction=aim_dir),),
                        daemon=True,
                        name=f"presence-aim-{aim_dir}",
                    ).start()
                else:
                    logger.debug("Presence aim restore: scene=%s has no aim -- skipping", state._active_scene)

            state.sensing_service = SensingService(
                camera_capture=state.camera_capture,
                input_device=AUDIO_SENSING_DEVICE if AUDIO_SENSING_DEVICE is not None else state.audio_input_device,
                poll_interval=float(os.environ.get("LELAMP_SENSING_INTERVAL", "2.0")),
                rgb_service=state.rgb_service,
                tts_service=state.tts_service,
                animation_service=state.animation_service,
                on_restore_aim=_presence_restore_aim,
                is_sleeping=lambda: state._sleeping,
            )
            state.sensing_service.start()
            logger.info("SensingService started")
        except Exception as e:
            logger.warning(f"SensingService failed to start: {e}")
            state.sensing_service = None

    # Start display (GC9A01 eyes)
    if DisplayService:
        try:
            state.display_service = DisplayService()
            state.display_service.start()
            logger.info("DisplayService started")
        except Exception as e:
            logger.warning(f"DisplayService failed to start: {e}")
            state.display_service = None

    # Object tracker (servo follow)
    from lelamp.service.tracking import TrackerService
    state.tracker_service = TrackerService()
    logger.info("TrackerService initialized")

    # GPIO17 button (single=stop/unmute, triple=reboot, long=shutdown)
    try:
        from lelamp.service.gpio_button import GPIOButtonHandler

        _gpio_button_handler = GPIOButtonHandler()
        _gpio_button_handler.start()
    except Exception as e:
        logger.warning(f"GPIO button init failed: {e}")

    # TTP223 capacitive touchpad (OrangePi sun60 only — same gestures as
    # GPIO button, runs independently. Skips silently on other boards.)
    try:
        from lelamp.service.ttp223 import TTP223Handler

        _ttp223_handler = TTP223Handler()
        _ttp223_handler.start()
    except Exception as e:
        logger.warning(f"TTP223 init failed: {e}")

    # Restore Bluetooth headset route if the user had one active before reboot.
    # Best effort — silent fallback to lamp speaker/mic if anything goes wrong.
    try:
        from lelamp.service.audio_route import maybe_restore_bt_route
        threading.Thread(
            target=maybe_restore_bt_route, daemon=True, name="bt-route-restore"
        ).start()
    except Exception as e:
        logger.warning(f"BT route restore scheduling failed: {e}")

    yield

    # Shutdown
    state._stop_current_effect()
    if state.display_service:
        state.display_service.stop()
    if state.music_service and state.music_service.playing:
        state.music_service.stop()

    if state.tracker_service and state.tracker_service.is_tracking:
        state.tracker_service.stop()

    _shutdown_threads = []
    if state.voice_service:
        _shutdown_threads.append(threading.Thread(target=state.voice_service.stop, daemon=True))
    if state.sensing_service:
        _shutdown_threads.append(threading.Thread(target=state.sensing_service.stop, daemon=True))
    for t in _shutdown_threads:
        t.start()
    for t in _shutdown_threads:
        t.join(timeout=6)

    if state.animation_service:
        state.animation_service._running.clear()
        if (
            state.animation_service._event_thread
            and state.animation_service._event_thread.is_alive()
        ):
            state.animation_service._event_thread.join(timeout=3.0)
    if state.rgb_service:
        state.rgb_service.stop()
    if state.camera_capture:
        state.camera_capture.stop()


app = FastAPI(
    title="LeLamp Hardware Runtime",
    description=(
        "Hardware driver API for Lumi AI Lamp. "
        "Controls servo motors (5-axis Feetech), RGB LEDs (64x WS2812), "
        "camera, audio (mic/speaker), display, and AI voice pipeline. "
        "Lumi Server (Go, port 5000) bridges requests here."
    ),
    version=(Path(__file__).parent / "VERSION_LELAMP").read_text().strip()
    if (Path(__file__).parent / "VERSION_LELAMP").exists()
    else "dev",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=[
        {
            "name": "Servo",
            "description": "5-axis Feetech servo motor control. Play pre-recorded animations or send direct joint positions.",
        },
        {
            "name": "LED",
            "description": "WS2812 RGB LED strip (64 LEDs). Set solid color, paint individual pixels, or turn off.",
        },
        {
            "name": "Camera",
            "description": "USB camera for snapshots and MJPEG streaming.",
        },
        {
            "name": "Audio",
            "description": "Low-level audio hardware control. Volume (amixer), raw recording (mic), and test tones. No AI -- just hardware.",
        },
        {
            "name": "Emotion",
            "description": "High-level orchestration: single call coordinates servo animation + LED color + display expression for an emotion.",
        },
        {
            "name": "Scene",
            "description": "Lighting scene presets (reading, focus, relax, movie, night, energize). Sets LED color temperature and brightness.",
        },
        {
            "name": "Presence",
            "description": "PIR motion sensor presence detection. Auto-dims lights when user is idle/away.",
        },
        {
            "name": "Display",
            "description": "Round LCD display: pixel art eye expressions (default) or info mode (time, weather, text).",
        },
        {
            "name": "Voice",
            "description": "AI voice pipeline. Deepgram STT (always-on listening) + LLM-based TTS (text-to-speech). Requires API keys.",
        },
        {
            "name": "Speaker",
            "description": "Per-user voice enrollment + recognition via cosine similarity on external-API embeddings.",
        },
        {
            "name": "System",
            "description": "Health checks and system status.",
        },
    ],
)

# --- Include route modules ---

from lelamp.routes import servo, led, camera, audio, emotion, scene, sensing, display, voice, music, system, bluetooth

app.include_router(servo.router)
app.include_router(led.router)
app.include_router(camera.router)
app.include_router(audio.router)
app.include_router(emotion.router)
app.include_router(scene.router)
app.include_router(sensing.router)
app.include_router(display.router)
app.include_router(voice.router)
app.include_router(music.router)
app.include_router(system.router)
app.include_router(bluetooth.router)

# Speaker recognition routes (lazy import — import failure tolerated so the
# rest of the server still boots if speaker deps are missing).
try:
    from lelamp.routes.speaker import router as speaker_router

    app.include_router(speaker_router)
except Exception as _speaker_import_err:  # noqa: BLE001
    logger.warning(
        "Speaker recognition router disabled: %s", _speaker_import_err
    )


class ProxyPrefixMiddleware:
    """ASGI middleware: reads X-Forwarded-Prefix and sets root_path."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            prefix = headers.get(b"x-forwarded-prefix", b"").decode()
            if prefix:
                scope["root_path"] = prefix
        await self.app(scope, receive, send)


app.add_middleware(ProxyPrefixMiddleware)


from ipaddress import ip_address, ip_network

from fastapi.responses import JSONResponse

_LOCAL_NETS = (
    ip_network("127.0.0.0/8"),
    ip_network("::1/128"),
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
)


def _is_local(value: str | None) -> bool:
    if not value:
        return False
    host = value.split(",")[0].strip()
    if host.startswith("[") and "]" in host:
        host = host[1 : host.index("]")]
    elif ":" in host and host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    try:
        addr = ip_address(host)
    except ValueError:
        return host == "localhost"
    return any(addr in net for net in _LOCAL_NETS)


def _is_same_origin(origin_or_referer: str | None, host: str) -> bool:
    if not origin_or_referer:
        return False
    # Strip scheme and path — just compare hostname:port
    value = origin_or_referer.split(",")[0].strip()
    for prefix in ("https://", "http://"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    value = value.split("/")[0]  # drop path
    return value == host


@app.middleware("http")
async def local_only_middleware(request, call_next):
    if MODE == "production":
        client = request.client.host if request.client else None
        xff = request.headers.get("x-forwarded-for")
        real_ip = request.headers.get("x-real-ip")

        # Localhost callers (Go server, OpenClaw on-device) always pass.
        if _is_local(client) and not (xff and not _is_local(xff)) and not (real_ip and not _is_local(real_ip)):
            return await call_next(request)

        # Browser requests from the same device origin pass (web UI, Swagger API calls).
        # /docs and /openapi.json are only reachable via iframe from the web UI —
        # direct URL navigation has no Referer and is blocked here intentionally.
        host = request.headers.get("host", "")
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        if _is_same_origin(origin, host) or _is_same_origin(referer, host):
            return await call_next(request)

        logger.warning(
            "Blocked external request: client=%s xff=%s origin=%s referer=%s path=%s",
            client, xff, origin, referer, request.url.path,
        )
        return JSONResponse(status_code=403, content={"detail": "LeLamp API: same-origin or local only"})
    return await call_next(request)


@app.middleware("http")
async def request_logging_middleware(request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.debug(
        "%s %s -> %d (%.1fms)",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


# --- System endpoints (stay in server.py) ---


@app.get("/version", tags=["System"])
def version():
    """Return LeLamp runtime version."""
    return {"version": app.version}


@app.get("/health", response_model=HealthResponse, tags=["System"])
def health():
    """Check which hardware drivers are available."""
    return {
        "status": "ok",
        "servo": state.animation_service is not None,
        "led": state.rgb_service is not None,
        "camera": state.camera_capture is not None,
        "audio": state.audio_output_device is not None or state.audio_input_device is not None,
        "sensing": state.sensing_service is not None,
        "voice": state.voice_service is not None and state.voice_service.available
        if state.voice_service
        else False,
        "tts": state.tts_service is not None and state.tts_service.available
        if state.tts_service
        else False,
        "music": state.music_service is not None and state.music_service.available
        if state.music_service
        else False,
        "display": state.display_service is not None,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HTTP_HOST, port=HTTP_PORT)
