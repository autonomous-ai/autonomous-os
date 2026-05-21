"""
Shared mutable state for the LeLamp server.

All service references, flags, and cross-route helpers live here so route
modules can import them without circular dependencies (routes never import
from server; server imports routes).
"""

import csv
import logging
import os
import threading
from typing import Optional

from lelamp.presets import (
    EMO_IDLE,
    EMOTION_PRESETS,
    FX_SPEAKING_WAVE,
    FX_SPEAKING_WAVE_RAINBOW,
    LST_EFFECT,
    LST_OFF,
    LST_SCENE,
    LST_SOLID,
    RGB_CMD_SOLID,
    SCENE_PRESETS,
)
from lelamp.service.rgb.effects import run_effect as _run_effect

logger = logging.getLogger("lelamp.server")

# --- Service references (set during lifespan) ---

animation_service = None
rgb_service = None
camera_capture = None
sensing_service = None
voice_service = None
display_service = None
tts_service = None
music_service = None
tracker_service = None

# --- Audio devices ---

audio_output_device: Optional[int] = None
audio_input_device: Optional[int] = None

# --- Camera state ---

_camera_disabled = False
_camera_manual_override = False

# --- LED effect state ---

_effect_thread: Optional[threading.Thread] = None
_effect_stop: threading.Event = threading.Event()
_effect_name: Optional[str] = None
_effect_base_color: Optional[tuple] = None
_active_scene: Optional[str] = None

# --- User LED state tracking (for emotion restore) ---

_user_led_state: Optional[dict] = None
_restore_timer: Optional[threading.Timer] = None
_sleeping: bool = False
_current_emotion: Optional[str] = None
# Fires release_servos after sleepy stays active continuously. Cancelled
# the moment the emotion changes away from sleepy (see routes/emotion.py).
_sleepy_release_timer: Optional[threading.Timer] = None

# --- TTS speaking LED state ---

_tts_speaking: bool = False

# --- Music playback LED state ---

_music_playing: bool = False

# --- Mic / Speaker mute state ---

_mic_muted = False
_mic_manual_override = False
_speaker_muted = False

# --- Snapshot state ---

_SNAPSHOT_DIR = os.environ.get(
    "LELAMP_SNAPSHOT_DIR", "/root/.openclaw/media/lumi-snapshots"
)
_SNAPSHOT_MAX = 20
_snapshot_paths: list = []

# --- Default user ---

DEFAULT_USER = os.environ.get("LELAMP_DEFAULT_USER", "unknown")

# --- OpenClaw workspace ---

_DEFAULT_AGENT_NAME = "lumi"
_OPENCLAW_WORKSPACE = os.environ.get("OPENCLAW_WORKSPACE", "/root/.openclaw/workspace")


# ---------------------------------------------------------------------------
# Cross-route helper functions (used by multiple route groups)
# ---------------------------------------------------------------------------


def _stop_current_effect():
    """Signal the running effect thread to stop and wait for it."""
    global _effect_thread, _effect_name, _effect_base_color
    if _effect_thread and _effect_thread.is_alive():
        _effect_stop.set()
        _effect_thread.join(timeout=2.0)
    _effect_thread = None
    _effect_name = None
    _effect_base_color = None


def _cancel_pending_restore():
    """Cancel any pending emotion restore timer."""
    global _restore_timer
    if _restore_timer is not None and _restore_timer.is_alive():
        _restore_timer.cancel()
        _restore_timer = None


def _save_user_led_state(state: dict):
    """Save the user-set LED state and cancel any pending emotion restore."""
    global _user_led_state
    logger.info("User LED state saved: %s", state)
    _user_led_state = state
    _cancel_pending_restore()


def _get_recording_duration(recording_name: str) -> float:
    """Return the playback duration (seconds) of a servo recording CSV."""
    recordings_dir = os.path.join(os.path.dirname(__file__), "recordings")
    path = os.path.join(recordings_dir, f"{recording_name}.csv")
    try:
        with open(path, newline="") as f:
            reader = csv.reader(f)
            next(reader)  # skip header
            rows = list(reader)
        if len(rows) < 2:
            return 3.0
        t0 = float(rows[0][0])
        t1 = float(rows[-1][0])
        return max(0.5, t1 - t0)
    except Exception:
        return 3.0


def _is_nonblack(color) -> bool:
    """Return True if color is a non-black RGB tuple/list (at least one channel > 0)."""
    return color and any(c > 0 for c in color)


def _get_current_led_color() -> tuple:
    """Return the current LED color for the speaking wave effect."""
    if _user_led_state:
        stype = _user_led_state.get("type")
        if stype == LST_SOLID and _is_nonblack(_user_led_state.get("color")):
            return tuple(_user_led_state["color"])
        if stype == LST_EFFECT and _is_nonblack(_user_led_state.get("color")):
            return tuple(_user_led_state["color"])
        if stype == LST_SCENE:
            preset = SCENE_PRESETS.get(_user_led_state.get("scene", ""))
            if preset:
                return tuple(int(c * preset["brightness"]) for c in preset["color"])
    if _is_nonblack(_effect_base_color):
        return _effect_base_color
    return (255, 180, 100)


def _get_user_base_color() -> tuple:
    """Return the user's current LED base color for overlay effects.

    Falls back to (0, 0, 0) when the strip has no active user state — pulse
    then behaves like the original wavefront-on-black animation.
    """
    if not _user_led_state:
        return (0, 0, 0)
    stype = _user_led_state.get("type")
    if stype == LST_OFF:
        return (0, 0, 0)
    if stype in (LST_SOLID, LST_EFFECT):
        color = _user_led_state.get("color")
        return tuple(color) if color else (0, 0, 0)
    if stype == LST_SCENE:
        preset = SCENE_PRESETS.get(_user_led_state.get("scene", ""))
        if preset:
            return tuple(int(c * preset["brightness"]) for c in preset["color"])
    return (0, 0, 0)


def _restore_user_led():
    """Restore LED to user state after emotion animation completes."""
    global _restore_timer
    _restore_timer = None

    if _tts_speaking:
        logger.info("LED restore: skipped -- TTS speaking_wave active")
        return

    if _music_playing:
        logger.info("LED restore: skipped -- music wave active")
        return

    if not rgb_service:
        return

    state = _user_led_state
    if state is None or state.get("type") == LST_OFF:
        logger.info(
            "LED restore: no active user state (state=%s) -- keeping emotion color",
            state,
        )
        return

    stype = state.get("type")
    logger.info("LED restore: restoring user state type=%s", stype)
    try:
        if stype == LST_SOLID:
            _stop_current_effect()
            rgb_service.dispatch(RGB_CMD_SOLID, tuple(state["color"]))
            logger.info("LED restore: solid color=%s", state["color"])
        elif stype == LST_EFFECT:
            _stop_current_effect()
            global _effect_thread, _effect_name, _effect_base_color
            color = tuple(state["color"])
            speed = state.get("speed", 1.0)
            effect = state["effect"]
            _effect_stop.clear()
            _effect_name = effect
            _effect_base_color = color
            _effect_thread = threading.Thread(
                target=_run_effect,
                args=(effect, color, speed, None, _effect_stop, rgb_service),
                daemon=True,
                name=f"led-restore-{effect}",
            )
            _effect_thread.start()
            logger.info(
                "LED restore: effect=%s color=%s speed=%s", effect, color, speed
            )
        elif stype == LST_SCENE:
            from lelamp.models import ServoAimRequest
            from lelamp.routes.servo import aim_servo

            preset = SCENE_PRESETS.get(state["scene"])
            if preset:
                _stop_current_effect()
                scaled = tuple(int(c * preset["brightness"]) for c in preset["color"])
                rgb_service.dispatch(RGB_CMD_SOLID, scaled)
                aim_dir = preset.get("aim")
                logger.info(
                    "LED restore: scene=%s color=%s aim=%s",
                    state["scene"],
                    scaled,
                    aim_dir,
                )
                if aim_dir and animation_service:
                    threading.Thread(
                        target=aim_servo,
                        args=(ServoAimRequest(direction=aim_dir),),
                        daemon=True,
                        name=f"restore-aim-{aim_dir}",
                    ).start()
            else:
                logger.warning(
                    "LED restore: scene=%s not found in SCENE_PRESETS", state["scene"]
                )
    except Exception as e:
        logger.warning("LED restore failed: %s", e)


def _schedule_led_restore(delay_s: float):
    """Schedule _restore_user_led to run after delay_s seconds."""
    global _restore_timer
    if _restore_timer is not None and _restore_timer.is_alive():
        _restore_timer.cancel()
    t = threading.Timer(delay_s, _restore_user_led)
    t.daemon = True
    t.start()
    _restore_timer = t


def _on_tts_speak_start():
    """Called by TTSService when TTS playback begins."""
    global _tts_speaking, _effect_thread, _effect_name, _effect_base_color
    global _restore_timer
    if not rgb_service:
        return

    color = _get_current_led_color()
    logger.info("TTS speaking LED start: color=%s", color)

    _tts_speaking = True

    if _restore_timer is not None and _restore_timer.is_alive():
        _restore_timer.cancel()
        _restore_timer = None

    _stop_current_effect()
    rgb_service.dispatch(RGB_CMD_SOLID, (0, 0, 0))

    _effect_stop.clear()
    _effect_name = FX_SPEAKING_WAVE
    _effect_base_color = color
    _effect_thread = threading.Thread(
        target=_run_effect,
        args=(FX_SPEAKING_WAVE, color, 2.5, None, _effect_stop, rgb_service),
        daemon=True,
        name="led-effect-speaking_wave",
    )
    _effect_thread.start()


def _on_tts_speak_end():
    """Called by TTSService when TTS playback finishes or is interrupted."""
    global _tts_speaking
    if not _tts_speaking:
        return

    _tts_speaking = False
    logger.info("TTS speaking LED end: stopping effect and restoring")

    _stop_current_effect()

    if rgb_service:
        rgb_service.dispatch(RGB_CMD_SOLID, (0, 0, 0))

    _restore_user_led()


def _on_music_play_start():
    """Called when MusicService starts streaming (ffmpeg has begun output)."""
    global _music_playing, _effect_thread, _effect_name, _effect_base_color
    global _restore_timer
    if not rgb_service:
        return
    if _tts_speaking:
        # TTS wave owns the strip; don't overwrite it.
        logger.info("Music wave skipped -- TTS speaking_wave active")
        return
    if _music_playing:
        return

    state = _user_led_state
    led_off = state is None or state.get("type") == LST_OFF
    if led_off:
        effect = FX_SPEAKING_WAVE_RAINBOW
        color = (0, 0, 0)  # ignored; each segment computes its own hue
        name = "led-music-speaking_wave_rainbow"
    else:
        effect = FX_SPEAKING_WAVE
        color = _get_current_led_color()
        name = "led-music-speaking_wave"
    logger.info("Music play LED start: effect=%s color=%s", effect, color)

    _music_playing = True

    if _restore_timer is not None and _restore_timer.is_alive():
        _restore_timer.cancel()
        _restore_timer = None

    _stop_current_effect()
    rgb_service.dispatch(RGB_CMD_SOLID, (0, 0, 0))

    _effect_stop.clear()
    _effect_name = effect
    _effect_base_color = color
    _effect_thread = threading.Thread(
        target=_run_effect,
        args=(effect, color, 2.5, None, _effect_stop, rgb_service),
        daemon=True,
        name=name,
    )
    _effect_thread.start()


def _on_music_play_end():
    """Called when MusicService finishes streaming (natural end, stop, or TTS preempt)."""
    global _music_playing
    if not _music_playing:
        return

    if _tts_speaking:
        # TTS wave already took over; clear flag but don't disturb the strip.
        logger.info("Music wave end deferred -- TTS speaking_wave owns strip")
        _music_playing = False
        return

    _music_playing = False
    logger.info("Music play LED end: stopping effect and restoring")

    _stop_current_effect()

    if rgb_service:
        rgb_service.dispatch(RGB_CMD_SOLID, (0, 0, 0))

    _restore_user_led()


def _apply_emotion_led_display(emotion: str, intensity: float = 1.0) -> Optional[list]:
    """Apply LED effect + display expression for an emotion. Returns scaled LED color or None."""
    preset = EMOTION_PRESETS.get(emotion)
    if not preset:
        return None
    if _tts_speaking:
        logger.info("Emotion LED skipped (%s) -- TTS speaking_wave active", emotion)
        if display_service:
            try:
                display_service.set_expression(emotion)
            except Exception as e:
                logger.warning("Emotion display failed: %s", e)
        return None
    led_color = None
    if emotion == EMO_IDLE and _user_led_state is not None:
        if display_service:
            try:
                display_service.set_expression(emotion)
            except Exception as e:
                logger.warning("Emotion display failed: %s", e)
        return None
    if rgb_service and preset.get("color"):
        scaled = [int(c * intensity) for c in preset["color"]]
        try:
            if preset.get("effect"):
                # Emotion-driven effects run on a black base, not the user's
                # ambient color: the agent is expressing a feeling and the
                # user should see it clearly. Overlay-on-user is reserved
                # for transient driver effects (e.g. Buddy busy pulse) via
                # the /led/effect transient=true path.
                _stop_current_effect()
                global _effect_thread, _effect_name, _effect_base_color
                _effect_stop.clear()
                _effect_name = preset["effect"]
                _effect_base_color = tuple(scaled)
                _effect_thread = threading.Thread(
                    target=_run_effect,
                    args=(
                        preset["effect"],
                        tuple(scaled),
                        preset.get("speed", 1.0),
                        None,
                        _effect_stop,
                        rgb_service,
                    ),
                    daemon=True,
                    name=f"led-emotion-{emotion}",
                )
                _effect_thread.start()
            else:
                rgb_service.dispatch(RGB_CMD_SOLID, tuple(scaled))
            led_color = scaled
            if sensing_service:
                sensing_service.presence.set_last_color(tuple(scaled))
        except Exception as e:
            logger.warning("Emotion LED failed: %s", e)
    if display_service:
        try:
            display_service.set_expression(emotion)
        except Exception as e:
            logger.warning("Emotion display failed: %s", e)
    return led_color


def _auto_camera_off(reason: str) -> bool:
    """Auto-disable camera. Respects manual override + active tracking."""
    global _camera_disabled
    if _camera_manual_override:
        logger.debug(
            "Auto camera off skipped -- manual override active (reason: %s)", reason
        )
        return False
    # Guard against sleepy-emotion / scene-change turning the camera off
    # mid-tracking. Tracker needs the frame stream, so any auto-off
    # triggered while tracking is active must be ignored.
    if tracker_service and tracker_service.is_tracking:
        logger.info("Auto camera off skipped -- tracking active (reason: %s)", reason)
        return False
    if not camera_capture or _camera_disabled:
        return False
    _camera_disabled = True
    camera_capture.stop()
    logger.info("Camera auto-disabled (reason: %s)", reason)
    return True


def _auto_camera_on(reason: str) -> bool:
    """Auto-enable camera. Respects manual override."""
    global _camera_disabled
    if _camera_manual_override:
        logger.debug(
            "Auto camera on skipped -- manual override active (reason: %s)", reason
        )
        return False
    if not camera_capture or not _camera_disabled:
        return False
    _camera_disabled = False
    camera_capture.start()
    logger.info("Camera auto-enabled (reason: %s)", reason)
    return True


def _read_agent_name(lumi_cfg: dict) -> str:
    """Read agent name from IDENTITY.md. Falls back to default 'lumi'."""
    identity_path = os.path.join(_OPENCLAW_WORKSPACE, "IDENTITY.md")
    try:
        with open(identity_path) as f:
            for line in f:
                lower = line.lower()
                idx = lower.find("**name:**")
                if idx >= 0:
                    name = (
                        line[idx + len("**name:**") :]
                        .strip()
                        .split("\u2014")[0]
                        .split("-")[0]
                        .strip()
                    )
                    if name:
                        return name.lower()
    except Exception:
        pass
    return _DEFAULT_AGENT_NAME


def _build_wake_words(name: str) -> list[str]:
    """Generate wake word variants from agent name."""
    n = name.lower()
    return [f"hey {n}", n, f"n\u00e0y {n}", f"\u00ea {n}", f"{n} \u01a1i"]


def _find_audio_device(output: bool = True) -> Optional[int]:
    """Find audio device index by known hardware names, with USB fallback."""
    try:
        import sounddevice as sd
    except ImportError:
        return None
    if not sd:
        return None
    output_names = ["seeed", "cd002"]
    input_names = ["seeed", "webcam"]
    input_skip = ["camera", "video"]
    names = output_names if output else input_names
    try:
        import re
        import subprocess

        devices = list(sd.query_devices())
        for keyword in names:
            for i, d in enumerate(devices):
                name = d["name"].lower()
                if keyword not in name:
                    continue
                if output and d["max_output_channels"] > 0:
                    return i
                if not output and d["max_input_channels"] > 0:
                    return i
        for i, d in enumerate(devices):
            name = d["name"].lower()
            if "usb" not in name:
                continue
            if not output and any(s in name for s in input_skip):
                continue
            if output and d["max_output_channels"] > 0:
                logger.info(
                    "Audio fallback: using USB device %d '%s' for output", i, d["name"]
                )
                return i
            if not output and d["max_input_channels"] > 0:
                logger.info(
                    "Audio fallback: using USB device %d '%s' for input", i, d["name"]
                )
                return i
        if not output:
            for i, d in enumerate(devices):
                name = d["name"].lower()
                if "usb" in name and d["max_input_channels"] > 0:
                    logger.info(
                        "Audio last-resort: using %d '%s' for input", i, d["name"]
                    )
                    return i
        alsa_cmd = ["aplay", "-l"] if output else ["arecord", "-l"]
        try:
            result = subprocess.run(alsa_cmd, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if not line.startswith("card "):
                        continue
                    m = re.search(r"card \d+: \S+ \[(.+?)\]", line)
                    if not m:
                        continue
                    card_label = m.group(1).lower()
                    if any(s in card_label for s in ("hdmi", "spdif", "iec958")):
                        continue
                    label_words = [w.lower() for w in m.group(1).split() if len(w) > 2]
                    for i, d in enumerate(devices):
                        dname = d["name"].lower()
                        if any(w in dname for w in label_words):
                            if output and d["max_output_channels"] > 0:
                                logger.info(
                                    "ALSA probe: device %d '%s' for output",
                                    i,
                                    d["name"],
                                )
                                return i
                            if not output and d["max_input_channels"] > 0:
                                logger.info(
                                    "ALSA probe: device %d '%s' for input", i, d["name"]
                                )
                                return i
        except Exception:
            pass
        skip = ["hdmi", "spdif", "iec958"]
        for i, d in enumerate(devices):
            dname = d["name"].lower()
            if any(s in dname for s in skip):
                continue
            if output and d["max_output_channels"] > 0:
                logger.info(
                    "Audio fallback (any): device %d '%s' for output", i, d["name"]
                )
                return i
            if not output and d["max_input_channels"] > 0:
                logger.info(
                    "Audio fallback (any): device %d '%s' for input", i, d["name"]
                )
                return i
    except Exception:
        pass
    return None
