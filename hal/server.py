"""
HAL Hardware Runtime -- FastAPI server on port 5001.

Only starts the drivers we need. LiveKit/OpenAI code stays untouched but never imported.
OS Server (Go, port 5000) bridges requests here.
"""

import json
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# Load .env BEFORE any hal imports so config.py reads correct env vars
load_dotenv(Path(__file__).parent / ".env", override=False)

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import hal.app_state as state
from hal.config import (
    AUDIO_INPUT_ALSA,
    AUDIO_OUTPUT_ALSA,
    AUDIO_SENSING_DEVICE,
    CAMERA_AUTO_EXPOSURE,
    CAMERA_BRIGHTNESS,
    CAMERA_EXPOSURE,
    CAMERA_GAIN,
    CAMERA_HEIGHT,
    CAMERA_INDEX,
    CAMERA_NAME,
    CAMERA_WIDTH,
    DL_API_KEY,
    HTTP_HOST,
    HTTP_PORT,
    DEVICE_ID,
    SERVO_FPS,
    SERVO_HOLD_S,
    SERVO_PLAY_RAMP_S,
    SERVO_PORT,
    TTS_SPEED,
    TTS_VOICE,
    TTS_INSTRUCTIONS,
    OS_CONFIG_PATH,
)
from hal.models import HealthResponse, StatusResponse
from hal.presets import SCENE_PRESETS, SERVO_CMD_PLAY
from hal.server_support.openapi_meta import API_DESCRIPTION, OPENAPI_TAGS

# --- Logging: colored stdout + rotating file (+ GELF) ---
from hal.server_support.log_setup import setup_logging

logger = setup_logging()


# --- Device declaration first: DEVICE.md decides which drivers we even import ---
# Driver imports are the expensive part of boot (cv2, onnx/torch model stacks —
# several seconds on an A523). Resolving the profile before them lets every
# import below be gated on the declared routes, so a device without the hardware
# never pays the import cost. plan_mounts semantics are unchanged: undeclared
# routes were skipped anyway; declared routes still use import success ==
# availability.


def _resolve_device_type() -> str:
    dev = os.environ.get("DEVICE_TYPE")
    if dev:
        return dev
    try:
        from hal.config import _os_cfg_get
        cfg = _os_cfg_get("device_type")
    except Exception:
        cfg = None
    if cfg:
        return cfg
    # No "lamp" fallback — refuse to boot the wrong body's drivers/soul/OTA.
    raise RuntimeError(
        "DEVICE_TYPE unresolved: set the DEVICE_TYPE env (provisioning) or "
        "config.json device_type — refusing to assume 'lamp'"
    )


def _devices_dir() -> str:
    # hal/server.py -> hal -> repo root (dev fallback; real installs always
    # set DEVICES_DIR, default /opt/devices)
    return os.environ.get("DEVICES_DIR") or os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "devices")
    )


def _device_profile():
    """This device's DeviceProfile. DEVICE.md is REQUIRED — a missing/unparseable
    one is a deploy fault, so fail loudly (no legacy "mount everything" fallback)."""
    from hal.board.device import load_device
    devices_dir = _devices_dir()
    try:
        return load_device(_resolve_device_type(), devices_dir)
    except Exception as e:
        raise RuntimeError(
            f"DEVICE.md required but not loaded for device '{_resolve_device_type()}' "
            f"(devices_dir={devices_dir}): {e}"
        ) from e


# DEVICE.md is required — _device_profile() fail-louds if it's missing/unparseable.
_profile = _device_profile()
# Full declared route surface (incl. `speaker`), keys usable with `in`.
_declared = _profile.declared_routes()

# Warm the heaviest driver chain (lerobot → torch, ~4s of the ~7.5s total import
# time on an A523) in parallel with the rest of the module imports below.
# Python's per-module import locks make the gated factory import further down
# wait on — not duplicate — this import, so it acts as a join point.
if "servo" in _declared:
    import importlib
    from hal.drivers.motors.factory import MOTION_DRIVERS

    _motion_cap = _profile.capabilities.get("motion")
    _motion_driver = _motion_cap.driver if _motion_cap else None
    _motion_entry = MOTION_DRIVERS.get(_motion_driver or "feetech")

    def _warm_import_servo():
        if _motion_entry:
            try:
                importlib.import_module(_motion_entry[0])
            except Exception:
                pass  # the factory import below reports the real error

    threading.Thread(target=_warm_import_servo, daemon=True, name="warm-import-servo").start()

# --- Lazy imports for hardware drivers (may not be available on dev machines),
# gated on the declared routes so undeclared hardware costs zero import time ---

AnimationService = None  # resolved motion service class (may be any MotionService impl)
RGBService = None
sd = None
np = None

if "servo" in _declared:
    from hal.drivers.motors.factory import resolve_motion_class
    _motion_cap = _profile.capabilities.get("motion")
    AnimationService = resolve_motion_class(
        _motion_cap.driver if _motion_cap else None,
        _motion_cap.required if _motion_cap else False,
    )
    if AnimationService is None:
        logger.warning("Servo motion service not available (driver: %s)",
                       _motion_cap.driver if _motion_cap else None)
else:
    logger.info("Servo drivers skipped — 'servo' not declared in DEVICE.md")

if "led" in _declared:
    try:
        from hal.drivers.rgb.rgb_service import RGBService
    except ImportError as e:
        logger.warning(f"LED drivers not available: {e}")
else:
    logger.info("LED drivers skipped — 'led' not declared in DEVICE.md")

try:
    import numpy as np
    import sounddevice as sd
except ImportError as e:
    logger.warning(f"Audio drivers not available: {e}")

cv2 = None
LocalVideoCaptureDevice = None
VideoCaptureDeviceInfo = None
resolve_camera_device_id = None
if "camera" in _declared:
    try:
        import cv2
    except ImportError as e:
        logger.warning(f"Camera drivers (opencv) not available: {e}")

    try:
        from hal.drivers.camera.factory import resolve_camera_class
        from hal.drivers.camera.models import VideoCaptureDeviceInfo
        from hal.drivers.camera.video_capture_device import resolve_camera_device_id

        # Same selector shape as motion: DEVICE.md picks the backend, because a
        # CSI sensor behind libcamera and a UVC webcam share no open path.
        _vision_cap = _profile.capabilities.get("vision")
        LocalVideoCaptureDevice = resolve_camera_class(
            _vision_cap.driver if _vision_cap else None,
            _vision_cap.required if _vision_cap else False,
        )
        if LocalVideoCaptureDevice is None:
            logger.warning("Camera capture device not available (driver: %s)",
                           _vision_cap.driver if _vision_cap else None)
    except ImportError as e:
        logger.warning(f"Video capture device not available: {e}")
else:
    logger.info("Camera drivers skipped — 'camera' not declared in DEVICE.md")

# --- Media owners: processes that hold this device's hardware ---------------
# A body that ships its own vendor runtime has that runtime holding the camera
# and the audio PCMs before HAL exists, and HAL cannot open what it does not
# own. Capabilities in that position declare `owner:` in DEVICE.md and this
# resolves each declared name to a handover class — same selector shape as
# `driver:` on motion and vision, so nothing here knows which body is running.
# One owner typically holds several capabilities (audio and vision both), so
# resolve by distinct name and release once each rather than once per
# capability.
_media_owners = []
for _owner_name in dict.fromkeys(
    c.owner for c in _profile.capabilities.values() if c.owner
):
    from hal.drivers.media_owner.factory import resolve_media_owner

    _owner_cls = resolve_media_owner(_owner_name)
    if _owner_cls is not None:
        _media_owners.append(_owner_cls())
        logger.info("Media owner '%s' declared — HAL will borrow the hardware", _owner_name)

SensingService = None
FacePerception = None
if "sensing" in _declared:
    try:
        from hal.drivers.sensing.perceptions.processors.facerecognizer_v2 import FacePerception
        from hal.drivers.sensing.sensing_service import SensingService
    except ImportError as e:
        logger.warning(f"Sensing service not available: {e}")
        SensingService = None
        FacePerception = None
else:
    logger.info("Sensing service skipped — 'sensing' not declared in DEVICE.md")

VoiceService = None
DeepgramSTT = None
AutonomousSTT = None
TTSService = None
PROVIDER_OPENAI = "openai"  # fallback when the TTS import below is skipped/unavailable
if "voice" in _declared:
    try:
        from hal.drivers.voice.stt import AutonomousSTT
        from hal.drivers.voice.stt import DeepgramSTT
        from hal.drivers.voice.voice_service import VoiceService
    except ImportError as e:
        logger.warning(f"Voice service not available: {e}")
else:
    logger.info("Voice service skipped — 'voice' not declared in DEVICE.md")

# TTS serves more than the voice route (music backchannel, sensing announcements,
# shutdown cue), so any declared audio-producing route pulls it in.
if {"voice", "audio", "music"} & set(_declared):
    try:
        from hal.drivers.voice.tts import TTSService
        from hal.drivers.voice.tts import PROVIDER_OPENAI
    except ImportError as e:
        logger.warning(f"TTS service not available: {e}")

MusicService = None
if "music" in _declared:
    try:
        from hal.drivers.voice.music_service import MusicService
    except ImportError as e:
        logger.warning(f"Music service not available: {e}")

DisplayService = None
if "display" in _declared:
    try:
        from hal.drivers.display.display_service import DisplayService
    except ImportError as e:
        logger.warning(f"Display service not available: {e}")

_gpio_button_handler = None
_ttp223_handler = None

# Set the moment lifespan shutdown begins — late async initializers (sensing-init)
# check it so they don't start services nobody will stop.
_lifespan_stopping = threading.Event()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _gpio_button_handler, _ttp223_handler

    # --- Phase 0: Borrow the hardware from whoever owns it ---
    # Empty unless DEVICE.md declares an `owner:`. Where one exists it holds
    # /dev/video* and both ALSA PCMs, and nothing below can succeed until it
    # lets go — the camera opens "busy", and PortAudio cannot probe a sample
    # rate, which resurfaces much later as TTS on output device -1 while every
    # status endpoint still reads healthy. Blocking, and first: a vendor SDK may
    # release as a side effect of connecting, but that runs in the motion-init
    # thread and races the audio detection in Phase 2.
    for _owner in _media_owners:
        _owner.release()

    # --- Phase 1: Fire slow hardware init in background threads ---

    def _init_servo():
        if not AnimationService:
            return
        # Declaration-driven: the servo route is mounted only when the device
        # declares the `motion` capability (DEVICE.md → motion: { routes: [servo] }).
        # Without it, starting AnimationService connects to a servo bus that isn't
        # there and every animation playback throws DeviceNotConnectedError. Gate on
        # the mount plan so a device that has no servo (e.g. intern-v2) never starts it.
        if "servo" not in _plan.mounted:
            logger.info("AnimationService skipped — device does not declare 'motion' (servo route not mounted)")
            return
        try:
            # Per-driver construction: the feetech backend needs the serial-bus
            # kwargs; SDK-backed drivers own their transport config (env-tunable).
            if (_motion_driver or "feetech") == "feetech":
                svc = AnimationService(
                    port=SERVO_PORT, lamp_id=DEVICE_ID, fps=SERVO_FPS,
                    duration=SERVO_PLAY_RAMP_S, hold_s=SERVO_HOLD_S,
                )
            else:
                svc = AnimationService()
            svc.start()
            state.animation_service = svc
            logger.info("Motion service started (%s)", type(svc).__name__)
        except Exception as e:
            logger.warning(f"Motion service failed to start: {e}")

    def _init_led():
        if not RGBService:
            return
        try:
            svc = RGBService(led_count=_led_count, safety_policy=_safety)
            svc.start()
            state.rgb_service = svc
            logger.info("RGBService started")
        except Exception as e:
            logger.warning(f"RGBService failed to start: {e}")

    def _init_camera():
        if not (LocalVideoCaptureDevice and VideoCaptureDeviceInfo and cv2):
            return
        # Gated like servo: the camera route mounts only when the device declares
        # `vision`. A device with no camera (e.g. intern-v2) must not try to open
        # one — otherwise it logs a misleading "Camera failed to start" hardware
        # warning when the camera was simply never part of the spec.
        if "camera" not in _plan.mounted:
            logger.info("Camera skipped — device does not declare 'vision' (camera route not mounted)")
            return
        try:
            # V4L2 index resolution is a UVC concern; a libcamera backend has no
            # node to look up, and probing would log a misleading failure.
            camera_device_id = (
                resolve_camera_device_id(CAMERA_NAME, CAMERA_INDEX)
                if LocalVideoCaptureDevice.requires_v4l2_index
                else CAMERA_INDEX
            )
            cap = LocalVideoCaptureDevice(
                VideoCaptureDeviceInfo(
                    device_id=camera_device_id,
                    max_width=CAMERA_WIDTH,
                    max_height=CAMERA_HEIGHT,
                    auto_exposure=CAMERA_AUTO_EXPOSURE,
                    exposure=CAMERA_EXPOSURE,
                    gain=CAMERA_GAIN,
                    brightness=CAMERA_BRIGHTNESS,
                )
            )
            if state._camera_disabled:
                # Disabled state restored from the camera sidecar: keep the
                # capture object (so /camera/enable can start it) but don't
                # open the sensor.
                logger.info("Camera capture created but not started -- disabled (restored)")
            else:
                cap.start()
            state.camera_capture = cap
            logger.info(
                f"Camera opened (device={camera_device_id}, {CAMERA_WIDTH}x{CAMERA_HEIGHT})"
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
        _out_env = os.environ.get("HAL_AUDIO_OUTPUT_DEVICE")
        if _out_env is not None:
            state.audio_output_device = int(_out_env)
            logger.info("Audio output device override from env: %d", state.audio_output_device)
        elif os.environ.get("HAL_AUDIO_OUTPUT_ALSA"):
            _alsa_out = os.environ["HAL_AUDIO_OUTPUT_ALSA"]
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
                # alias that points at it (e.g. plug:device_speaker). Force a
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

    # Auto-start voice pipeline from os-server config
    os_config_path = OS_CONFIG_PATH
    try:
        with open(os_config_path) as f:
            os_cfg = json.load(f)
        dgk = os_cfg.get("deepgram_api_key", "")
        llm_key = os_cfg.get("llm_api_key", "")
        llm_url = os_cfg.get("llm_base_url", "")
        voice = os_cfg.get("tts_voice", "") or TTS_VOICE
        tts_provider = os_cfg.get("tts_provider", PROVIDER_OPENAI)
        if llm_key and llm_url and TTSService and not state.tts_service:
            state.tts_service = TTSService(
                api_key=llm_key,
                base_url=llm_url,
                sound_device_module=sd,
                numpy_module=np,
                output_device=state.audio_output_device,
                voice=voice,
                speed=TTS_SPEED,
                instructions=os_cfg.get("tts_instructions", "") or TTS_INSTRUCTIONS or None,
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
            agent_name = state._read_agent_name()
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
                stt_model = (os_cfg.get("stt_model") or "").strip() or None
                stt_language = (os_cfg.get("stt_language") or "").strip() or None
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
                    # `audio` (the mic) gates VOICE people perception: speaker-ID
                    # and speech emotion (reading the user's emotion from voice)
                    # need only a mic, not a camera or the presence people-layer —
                    # so any device with a mic runs them. (Face emotion in the
                    # sensing loop stays `presence`-gated; see SensingService below.)
                    enable_people_perception=("audio" in _profile.capabilities),
                    # `expression` (LED+servo face) gates the realtime agent's
                    # express_emotion tool. A device with no face never registers
                    # the tool, so the realtime model can't set an emotion.
                    enable_expression=("expression" in _profile.capabilities),
                )
                if state._mic_muted:
                    # Mute restored from the sidecar (or the physical switch
                    # applied it during driver init): build the pipeline but
                    # don't open the mic — same guard as routes/voice.py
                    # start_voice. Without this the boot auto-start reopened
                    # the mic on every HAL restart while "muted".
                    logger.info("VoiceService created but NOT started -- mic muted")
                else:
                    state.voice_service.start()
                    logger.info("VoiceService auto-started (%s, wake_words=%s)", stt_provider.name, wake_words)
    except FileNotFoundError:
        logger.info(
            f"os-server config not found at {os_config_path}, voice will wait for /voice/start"
        )
    except Exception as e:
        logger.warning(f"Auto-start voice from os-server config failed: {e}")

    # Start music service
    if MusicService:
        try:
            from hal.routes.music import _on_music_complete

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
            from hal.routes.music import _backchannel_pool
            for phrase in _backchannel_pool():
                state.tts_service.speak_cached(phrase, prerender=True)
        except Exception as e:
            logger.warning("Music backchannel prerender failed: %s", e)
        # Also warm the rate-limit notice so it can play from cache (no API call)
        # when the TTS provider later returns 429 / quota-exhausted mid-turn.
        # (Go-owned notices — e.g. the LLM-limit phrase — warm themselves via
        # /voice/speak prerender=true from the os-server side.)
        try:
            from hal.i18n import PHRASE_RATE_LIMIT, localized_phrase

            notice = localized_phrase(PHRASE_RATE_LIMIT)
            if notice:
                state.tts_service.speak_cached(notice, prerender=True)
        except Exception as e:
            logger.warning("Rate-limit notice prerender failed: %s", e)

    threading.Thread(
        target=_prerender_music_backchannel,
        daemon=True,
        name="prerender-music-backchannel",
    ).start()

    # --- Phase 3: Wait for hardware threads, then start hardware-dependent services ---
    for t in hw_threads:
        t.join(timeout=10)

    # Start sensing loop
    sensing_enabled = os.environ.get("HAL_SENSING_ENABLED", "true").lower() in (
        "true",
        "1",
        "yes",
    )
    # Constructing SensingService opens the remote perception WS channels
    # (motion/pose/fire-hazard/emotion encryption handshakes) sequentially —
    # ~4-5s on device. Run it in a background thread so it doesn't hold up
    # "Application startup complete"; every /sensing route already degrades
    # gracefully while state.sensing_service is still None.
    def _start_sensing():
        try:
            def _presence_restore_aim():
                """Re-aim the device to active scene direction when presence restores light."""
                if not state._active_scene:
                    logger.info("Presence aim restore: no active scene -- skipping aim")
                    return
                if not state.animation_service:
                    logger.warning("Presence aim restore: animation_service not available")
                    return
                # Imported here, not at _start_sensing top: on sensing devices
                # without a servo this pulls the whole motors stack (and an
                # import failure there must not kill SensingService).
                from hal.routes.servo import aim_servo
                from hal.models import ServoAimRequest
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

            # `presence` capability gates the people-perception loop: face
            # identity + facial emotion (ML over the camera via perception-service). A
            # device with a camera but no `presence` (it only streams / does
            # motion) must not run those models. Declaration-driven, not env.
            _has_presence = "presence" in _profile.capabilities
            if _lifespan_stopping.is_set():
                logger.info("sensing-init: shutdown already in progress — skipping")
                return
            svc = SensingService(
                camera_capture=state.camera_capture,
                # Declared or absent, never guessed. This used to fall back to
                # the voice mic, which is only ever right by luck: on a device
                # whose speaker and mic are one card, sound sensing then opened
                # the raw PortAudio index for a card the 16 kHz capture already
                # held, exclusively and at 44100 Hz. It failed several times a
                # minute, filled the journal with pa_linux_alsa "AlsaOpen
                # failed", and reported no sound the whole time — a feature that
                # looks configured and does nothing. Passing None instead lets
                # the orchestrator's existing gate skip SoundPerception and say
                # so, matching DEVICE-SPEC.md: undeclared is not a default, it
                # is an absence. Every shipping device declares
                # HAL_AUDIO_SENSING_DEVICE, so none of them loses the feature.
                input_device=AUDIO_SENSING_DEVICE,
                poll_interval=float(os.environ.get("HAL_SENSING_INTERVAL", "2.0")),
                rgb_service=state.rgb_service,
                tts_service=state.tts_service,
                animation_service=state.animation_service,
                on_restore_aim=_presence_restore_aim,
                is_sleeping=lambda: state._sleeping,
                enable_people_perception=_has_presence,
            )
            # The ~4-5s constructor may finish after shutdown began — the
            # shutdown path's `if state.sensing_service` check has already
            # passed by then, so nobody would stop it. Don't start it.
            if _lifespan_stopping.is_set():
                logger.info("sensing-init: shutdown began during construction — not starting")
                return
            state.sensing_service = svc
            svc.start()
            logger.info("SensingService started (people_perception=%s via presence capability)", _has_presence)
        except Exception as e:
            logger.warning(f"SensingService failed to start: {e}")
            state.sensing_service = None

    if SensingService and sensing_enabled:
        threading.Thread(target=_start_sensing, daemon=True, name="sensing-init").start()

    # Start display (GC9A01 eyes)
    if DisplayService:
        try:
            state.display_service = DisplayService()
            state.display_service.start()
            logger.info("DisplayService started")
        except Exception as e:
            logger.warning(f"DisplayService failed to start: {e}")
            state.display_service = None

    # Object tracker (servo follow) — needs both a camera to see with and a
    # servo to follow with; every route touching state.tracker_service is
    # None-tolerant, so devices without either never load the tracker stack.
    if "servo" in _plan.mounted and "camera" in _plan.mounted:
        from hal.drivers.tracking import TrackerService
        state.tracker_service = TrackerService()
        logger.info("TrackerService initialized")
    else:
        logger.info("TrackerService skipped — needs servo+camera routes mounted")

    # GPIO17 button (single=stop/unmute, triple=reboot, long=shutdown)
    try:
        from hal.drivers.gpio_button import GPIOButtonHandler

        _gpio_button_handler = GPIOButtonHandler()
        _gpio_button_handler.start()
    except Exception as e:
        logger.warning(f"GPIO button init failed: {e}")

    # TTP223 capacitive touchpad (OrangePi sun60 only — same gestures as
    # GPIO button, runs independently. Skips silently on other boards.)
    try:
        from hal.drivers.ttp223 import TTP223Handler

        _ttp223_handler = TTP223Handler()
        _ttp223_handler.start()
    except Exception as e:
        logger.warning(f"TTP223 init failed: {e}")

    # Dedicated mic-mute push button (OrangePi sun60 PE1 today). One press =
    # one mute↔unmute flip via HAL voice routes. Silent skip on boards that
    # don't declare `mic_button` in boards.json.
    try:
        from hal.drivers.mic_button import MicButtonHandler

        _mic_button_handler = MicButtonHandler()
        _mic_button_handler.start()
    except Exception as e:
        logger.warning(f"Mic button init failed: {e}")

    # Restore Bluetooth headset route if the user had one active before reboot.
    # Best effort — silent fallback to the device speaker/mic if anything goes wrong.
    try:
        from hal.drivers.audio_route import maybe_restore_bt_route
        threading.Thread(
            target=maybe_restore_bt_route, daemon=True, name="bt-route-restore"
        ).start()
    except Exception as e:
        logger.warning(f"BT route restore scheduling failed: {e}")

    # Re-apply the scene that was active before a service restart (boot-scoped
    # sidecar) so the agent's belief ("focus mode is on") stays true across
    # HAL restarts instead of desyncing from a scene-less HAL.
    try:
        from hal.routes.scene import restore_persisted_scene
        threading.Thread(
            target=restore_persisted_scene, daemon=True, name="scene-restore"
        ).start()
    except Exception as e:
        logger.warning(f"Scene restore scheduling failed: {e}")

    # Mic mute restored from the sidecar (or already applied by the physical
    # switch during driver init): paint the mic-muted LED indicator now that
    # the RGB service is up. _start_mic_muted_effect (not _apply_): the flag
    # is already set, and it self-guards on scene/LED-off via owns_strip.
    if state._mic_muted:
        try:
            state._start_mic_muted_effect()
        except Exception as e:
            logger.warning(f"Mic-muted LED repaint failed: {e}")

    # Thermal fail-safe monitor (only when `thermal` bounds are declared).
    if _safety and _safety.thermal:
        threading.Thread(
            target=_thermal_monitor, args=(_safety,), daemon=True, name="thermal-monitor"
        ).start()
        logger.info(
            "Thermal monitor: max_temp_c=%d resume_temp_c=%d",
            _safety.thermal.max_temp_c, _safety.thermal.resume_temp_c,
        )

    yield

    _lifespan_stopping.set()
    _thermal_stop.set()

    # Voice/sensing stops (~3s) run concurrently with the announce+park below —
    # they only tear down mic/STT/perception threads, never the TTS output the
    # cue plays on.
    _shutdown_threads = []
    if state.voice_service:
        _shutdown_threads.append(threading.Thread(target=state.voice_service.stop, daemon=True))
    if state.sensing_service:
        _shutdown_threads.append(threading.Thread(target=state.sensing_service.stop, daemon=True))
    for t in _shutdown_threads:
        t.start()

    # Shutdown — announce + park servos first (only when OS is actually
    # going down and no button path already announced), so the audible cue
    # fires while tts_service is still alive.
    from hal.drivers.os_shutdown import announce_os_shutdown
    announce_os_shutdown()

    state._stop_current_effect()
    if state.display_service:
        state.display_service.stop()
    if state.music_service and state.music_service.playing:
        state.music_service.stop()

    if state.tracker_service and state.tracker_service.is_tracking:
        state.tracker_service.stop()

    for t in _shutdown_threads:
        # Best-effort grace only — systemd kills the whole cgroup (arecord
        # included) right after, so a slow voice/sensing stop must not add
        # seconds to every restart.
        t.join(timeout=3)

    if state.animation_service:
        # MotionService.stop(), not the AnimationService internals this used to
        # clear by hand: `_running` and `_event_thread` belong to the feetech
        # backend, so on any other one the teardown died here with
        # AttributeError and uvicorn logged "Application shutdown failed.
        # Exiting." — abandoning every line below it. On a Reachy that cost the
        # SDK its goto_sleep and client.disconnect, left the daemon holding a
        # dead client socket, and skipped the media handover entirely, so
        # Pollen's stack stayed deaf and blind after every HAL restart.
        # Wrapped because shutdown must not raise: whatever a driver does here,
        # the LEDs, the camera and the handover below still have to run.
        try:
            state.animation_service.stop(timeout=3.0)
        except Exception as e:
            logger.warning(f"Motion service stop failed: {e}")
    if state.rgb_service:
        state.rgb_service.stop()
    if state.camera_capture:
        state.camera_capture.stop()

    # Give the hardware back last — after voice, sensing and the camera have all
    # closed their handles, so the owner can actually reopen them. Restarting
    # HAL without this leaves the vendor runtime deaf and blind.
    for _owner in _media_owners:
        _owner.acquire()


app = FastAPI(
    title="HAL Hardware Runtime",
    description=API_DESCRIPTION,
    version=(Path(__file__).parent / "VERSION_HAL").read_text().strip()
    if (Path(__file__).parent / "VERSION_HAL").exists()
    else "dev",
    lifespan=lifespan,
    # Built-in /docs disabled; a custom handler below serves the Swagger HTML
    # without inline <script> so the OS server nginx can keep CSP `script-src 'self'`
    # (no `'unsafe-inline'`). /redoc stays on the default since it's not the
    # endpoint the in-iframe browser flow uses.
    docs_url=None,
    redoc_url="/redoc",
    # `servers` tells Swagger UI which base URL to prepend on "Try it out".
    # In the browser context the iframe lives at /api/hardware/docs and admin
    # auth gates /api/hardware/* via the OS server's reverse proxy; in the loopback /
    # SSH-tunnel context calls go directly to HAL. Operator can switch
    # between them via the Swagger UI dropdown.
    servers=[
        {"url": "/api/hardware", "description": "Via OS server admin proxy (browser)"},
        {"url": "/", "description": "Direct (loopback / SSH tunnel)"},
    ],
    openapi_tags=OPENAPI_TAGS,
)

# --- Include route modules (declaration-driven via DEVICE.md) ---
# Mount routes by crossing what this device's DEVICE.md *declares* with which
# drivers are actually *available* (importable), via hal.board.device.plan_mounts.
# A device is "the device minus motion+display" by declaring fewer capabilities — not by
# forking. Per devices/contract/DEVICE-SPEC.md the boot rule is:
#   declared + available            -> mount
#   declared + required + missing    -> FAIL LOUD in production (a hardware fault)
#   declared + optional  + missing    -> skip (graceful degradation)
#   undeclared                       -> skip (a different device, by design)
# Falls back to mounting everything when no DEVICE.md is found, so existing
# deployments are unaffected. See devices/contract/DEVICE-SPEC.md and hal/board/device.py.

# Route modules import their own driver stacks, so importing all 12
# unconditionally would defeat the declaration-gated driver imports above.
# Undeclared hardware routes are never mounted (plan_mounts only consults
# declared routes), so skipping their module import changes nothing else.
# The driverless routes (emotion/scene/system/bluetooth) import cheaply and
# other code calls into them in-process, so they always load.
import importlib

_ALWAYS_ROUTES = ("audio", "emotion", "scene", "system", "bluetooth")
_ROUTERS_BY_NAME = {}
for _rname in (
    "servo", "led", "camera", "audio", "emotion", "scene", "sensing",
    "display", "voice", "music", "system", "bluetooth",
):
    if _rname not in _declared and _rname not in _ALWAYS_ROUTES:
        logger.info("Route module '%s' skipped — not declared in DEVICE.md", _rname)
        continue
    _ROUTERS_BY_NAME[_rname] = importlib.import_module(f"hal.routes.{_rname}").router

# Speaker recognition imports separately — its deps (face/speaker embedding
# models) are heavy and may be absent. It's a declared `speaker` route under the
# audio capability (devices/*/DEVICE.md), so it joins the SAME declaration gate
# below: import success == availability, no separate bypass mount.
if "speaker" in _declared:
    try:
        from hal.routes.speaker import router as _speaker_router

        _ROUTERS_BY_NAME["speaker"] = _speaker_router
    except Exception as _speaker_import_err:  # noqa: BLE001
        logger.warning("Speaker recognition router unavailable: %s", _speaker_import_err)


# Mount-time driver availability: "is this route's driver code importable on this
# machine". The lazy driver-class imports near the top of this file already set
# each global to None on ImportError. Hardware-connection faults (cable unplugged)
# surface later in lifespan() as warnings — they can't abort the mount because
# lifespan runs after app construction. Routes with no import-time driver
# dependency are always mountable; their handlers degrade if the service is absent.
_route_available = {
    "servo": AnimationService is not None,
    "led": RGBService is not None,
    "camera": cv2 is not None and LocalVideoCaptureDevice is not None and VideoCaptureDeviceInfo is not None,
    "audio": sd is not None,
    "voice": VoiceService is not None,
    "sensing": SensingService is not None,
    "display": DisplayService is not None,
    "music": MusicService is not None,
    "emotion": True, "scene": True, "system": True, "bluetooth": True,
    "speaker": "speaker" in _ROUTERS_BY_NAME,
}

# Safety bounds (SAFETY.md front matter) resolved once at boot, below the brain.
# Pass-through when absent (light fail-safe); a present-but-malformed schema
# fail-louds inside load_safety, like DEVICE.md. Slice 1 = light.max_brightness.
from hal.safety.policy import load_safety
_safety = load_safety(os.path.join(_devices_dir(), _resolve_device_type()), _profile.safety_ref)
state.safety_policy = _safety  # route-level gates (e.g. music quiet hours) read it here

# Per-device preset overlay: deep-merge devices/<type>/presets.json onto the base
# EMOTION/SCENE/AIM tables in place (a device declares only the look/behaviour
# values it wants different) and resolve the LED ring size. Runs at import, before
# lifespan builds RGBService and before any route reads a preset. No file → base
# presets verbatim and the default LED count.
from hal.board.presets_overlay import apply_device_presets
_led_count = apply_device_presets(_resolve_device_type(), _devices_dir())
logger.info(
    "Safety policy: device=%s max_brightness=%s light_quiet=%s audio_quiet=%s",
    _resolve_device_type(),
    _safety.max_brightness if _safety else None,
    bool(_safety and _safety.light_quiet),
    bool(_safety and _safety.audio_quiet),
)


def _safety_view(p):
    """Serialize the resolved SafetyPolicy for GET /device (null when no bounds)."""
    if p is None:
        return None

    def _qh(q):
        if q is None:
            return None
        d = {"start": q.start.strftime("%H:%M"), "end": q.end.strftime("%H:%M")}
        if q.max_brightness is not None:
            d["max_brightness"] = q.max_brightness
        return d

    light = {}
    if p.max_brightness is not None:
        light["max_brightness"] = p.max_brightness
    if p.light_quiet is not None:
        light["quiet_hours"] = _qh(p.light_quiet)
    out = {}
    if light:
        out["light"] = light
    if p.audio_quiet is not None:
        out["audio"] = {"quiet_hours": _qh(p.audio_quiet)}
    if p.motion is not None:
        m = {"stop_always": p.motion.stop_always}
        if p.motion.max_speed is not None:
            m["max_speed"] = p.motion.max_speed
        out["motion"] = m
    if p.thermal is not None:
        out["thermal"] = {
            "max_temp_c": p.thermal.max_temp_c,
            "resume_temp_c": p.thermal.resume_temp_c,
        }
    return out or None


# Thermal fail-safe monitor — a background daemon that reads SoC temperature and,
# when `thermal` bounds are declared, raises a health event + stops discretionary
# motion (tracking) on over-temp, clearing on cool-down (hysteresis). Only started
# when _safety.thermal is set (presence-driven; off otherwise). The CPU heat isn't
# the servo's fault, so we don't freeze idle — same posture as the network reflex.
_thermal_stop = threading.Event()


def _thermal_monitor(policy, interval: float = 10.0):
    from hal.safety.policy import read_soc_temp_c, thermal_over
    while not _thermal_stop.is_set():
        temp = read_soc_temp_c()
        state.soc_temp_c = temp
        over = thermal_over(policy, temp, state.thermal_over)
        if over and not state.thermal_over:
            state.thermal_over = True
            logger.warning(
                "[thermal] SoC %.1f°C >= %d°C — over-temp; stopping discretionary motion",
                temp, policy.thermal.max_temp_c,
            )
            try:
                if state.tracker_service and state.tracker_service.is_tracking:
                    state.tracker_service.stop()
            except Exception as e:
                logger.warning("[thermal] stop tracking failed: %s", e)
        elif state.thermal_over and not over:
            state.thermal_over = False
            logger.info(
                "[thermal] SoC %s°C <= %d°C — recovered",
                f"{temp:.1f}" if temp is not None else "?", policy.thermal.resume_temp_c,
            )
        _thermal_stop.wait(interval)


def _thermal_view():
    """Thermal status for GET /health — null when no `thermal` bound is declared."""
    if not (_safety and _safety.thermal):
        return None
    return {
        "over": state.thermal_over,
        "temp_c": state.soc_temp_c,
        "max_temp_c": _safety.thermal.max_temp_c,
    }

# Board gate: refuse to boot on hardware this device doesn't declare in
# DEVICE.md `boards`. Wrong/unknown board → wrong pin maps → fail loud before we
# mount any actuating route (raw match, so the default_board fallback can't mask
# an unsupported board).
from hal.board.board import assert_board_supported
_board_id = assert_board_supported(_profile.boards)
logger.info("Board gate: device=%s board=%s declared=%s", _resolve_device_type(), _board_id, _profile.boards)

from hal.board.device import plan_mounts

# _declared (full surface incl. `speaker`) resolved at the top of this file,
# before the driver imports it gates. Availability = driver importable.
_available = {r: _route_available.get(r, False) for r in _declared}
_plan = plan_mounts(_declared, _available)
logger.info(
    "Declaration-driven mount plan: device=%s mounted=%s skipped=%s failed_required=%s",
    _resolve_device_type(), _plan.mounted, _plan.skipped, _plan.failed_required,
)
# Spec rule #3: a required capability whose driver can't import is a real
# fault — abort loudly in EVERY mode. Dev runs on real Pi hardware too, so
# there is no off-hardware case to spare. Optional routes simply skip; a
# declared route HAL has no router for is treated as unavailable (→ fail if
# required, skip if optional).
if not _plan.ok:
    raise RuntimeError(
        f"Device '{_resolve_device_type()}' requires routes whose drivers are "
        f"unavailable: {_plan.failed_required}. Fix the driver/hardware, or mark "
        f"the capability optional in devices/{_resolve_device_type()}/DEVICE.md."
    )
for _name in _plan.mounted:
    app.include_router(_ROUTERS_BY_NAME[_name])

# Self-hosted Swagger UI assets. The OS server nginx CSP keeps `script-src 'self'` so
# the bundled JS/CSS load from this same origin (no cdn.jsdelivr.net). The
# /docs handler below serves the HTML; its <script> tags reference these
# files via relative paths.
_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
else:
    logger.warning("Swagger UI static dir missing: %s", _STATIC_DIR)


@app.get("/docs", include_in_schema=False)
def custom_swagger_ui() -> HTMLResponse:
    """Serve Swagger UI with no inline <script>.

    Built-in `app.docs_url` injects an inline `<script>const ui = SwaggerUIBundle(...)</script>`
    block which forces the OS server nginx CSP to allow `'unsafe-inline'` for scripts.
    Externalising the init into `/static/swagger-init.js` lets the CSP stay
    strict (`script-src 'self'`). Relative URLs (`./openapi.json`,
    `./static/...`) make the page work both via the OS server proxy iframe and
    direct loopback access.
    """
    html = (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"  <title>{app.title} - Swagger UI</title>\n"
        '  <link rel="stylesheet" href="./static/swagger-ui.css">\n'
        "</head>\n"
        "<body>\n"
        '  <div id="swagger-ui"></div>\n'
        '  <script src="./static/swagger-ui-bundle.js"></script>\n'
        '  <script src="./static/swagger-init.js"></script>\n'
        "</body>\n"
        "</html>\n"
    )
    return HTMLResponse(content=html)


from hal.server_support.http_security import (
    ProxyPrefixMiddleware,
    local_only_middleware,
    request_logging_middleware,
)

# Middleware registration order preserved exactly as before the extraction to
# hal/http_security.py: ProxyPrefix first (sets root_path from
# X-Forwarded-Prefix), then the local-only / same-origin / bearer gate, then
# request logging. Identical Starlette stack — only the bodies moved out.
app.add_middleware(ProxyPrefixMiddleware)
app.middleware("http")(local_only_middleware)
app.middleware("http")(request_logging_middleware)


# --- System endpoints (stay in server.py) ---


@app.get("/version", tags=["System"])
def version():
    """Return HAL runtime version."""
    return {"version": app.version}


@app.get("/device", tags=["System"])
def device():
    """This device's identity from DEVICE.md (id/name/type/schema) plus the
    board the runtime resolved and the capability routes it mounted."""
    return {
        "id": _profile.id,
        "name": _profile.name,
        "type": _profile.type,
        "schema": _profile.schema,
        "board": _board_id,
        "boards": _profile.boards,
        "safety_ref": _profile.safety_ref,
        # Resolved, enforced safety bounds (not just the ref): brightness ceiling +
        # quiet-hours windows. null when the device declares no machine bounds.
        "safety": _safety_view(_safety),
        "memory": {"backend": _profile.memory_backend} if _profile.memory_backend else None,
        "routes": sorted(_plan.mounted),
        # Declared implementation families (informational hardware manifest; the
        # route is the contract, the driver behind it is free to change).
        "drivers": {g: c.driver for g, c in _profile.capabilities.items() if c.driver},
    }


@app.get("/health", response_model=HealthResponse, tags=["System"])
def health():
    """Check which hardware drivers are available."""
    return {
        "status": "ok",
        "servo": state.animation_service is not None and state.animation_service.is_connected,
        "led": state.rgb_service is not None and state.rgb_service._driver is not None,
        "camera": state.camera_capture is not None and state.camera_capture.last_frame is not None,
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
        "thermal": _thermal_view(),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HTTP_HOST, port=HTTP_PORT)
