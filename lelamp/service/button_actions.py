"""Shared button/touch actions.

Reused by any input device that maps to the same three gestures:
- single_click_action(): stop speaker / unmute mic + announce listening
- triple_click_action(): reboot OS
- long_press_action():  shutdown OS

Callers (GPIO button, touchpad, future remotes) only need to detect the
gesture and invoke the matching function — the destructive sequencing
(TTS announce → servo park → shutdown/reboot) lives here so every input
path gets the same safe behavior.
"""

import logging
import random
import subprocess
import threading
import time

import requests

import lelamp.app_state as state
from lelamp.i18n import (
    HEAD_PAT_PHRASES_BY_LANG,
    PHRASE_LISTENING,
    PHRASE_REBOOT,
    PHRASE_SHUTDOWN,
    PHRASES_BY_LANG,
)
from lelamp.presets import DEFAULT_LANG

logger = logging.getLogger(__name__)

DOUBLE_CLICK_WINDOW = 0.4  # seconds to wait for second click
LONG_PRESS_DURATION = 5.0  # seconds held → shutdown on release
FACTORY_RESET_DURATION = 10.0  # seconds held → factory-reset on release (supersedes shutdown)

# Lamp Go sensing endpoint. Head-pat notify is fire-and-forget — Lamp
# Go appends a NO_REPLY hint so the agent records the event in
# conversation history without speaking back.
LAMP_SENSING_URL = "http://127.0.0.1:5000/api/sensing/event"


def _notify_head_pat(spoken: str):
    """Tell Lamp Go that the lamp was just stroked. Called from the
    head-pat TTS thread *after* speak_cached actually played a phrase,
    so the rate is bounded by phrase playback (~1-3s) — no extra
    debounce needed. TTS-busy strokes are dropped silently and never
    notify, which is the right behaviour: the agent only learns about
    petting moments the user actually heard a response to.

    `spoken` is the exact phrase Lamp just said (incl. eleven_v3 audio
    tags like [laughs] / [whispers]) so the agent can read Lamp's tone
    and weave it into memory — "I laughed and said tickles" lands
    differently than "I sighed and asked them to stop"."""
    try:
        requests.post(
            LAMP_SENSING_URL,
            json={
                "type": "touch.head_pat",
                "message": f'Lamp was petted and responded: "{spoken}"',
            },
            timeout=0.5,
        )
    except Exception:
        pass


def _current_lang() -> str:
    try:
        from lelamp.config import _lamp_cfg_get
        return (_lamp_cfg_get("stt_language") or "").strip()
    except Exception:
        return ""


def _phrase(key: str) -> str:
    """Return the localized phrase for `key` based on Lamp's stt_language.
    Falls back to DEFAULT_LANG when the config can't be read or the
    language is empty/unknown."""
    pool = PHRASES_BY_LANG.get(key, {})
    return pool.get(_current_lang()) or pool.get(DEFAULT_LANG, "")


def _random_head_pat_phrase() -> str:
    """Pick a random pet-response phrase for the current language."""
    pool = (
        HEAD_PAT_PHRASES_BY_LANG.get(_current_lang())
        or HEAD_PAT_PHRASES_BY_LANG.get(DEFAULT_LANG, [])
    )
    return random.choice(pool) if pool else ""


def _announce_listening():
    """Speak the localized listening cue, preempting any in-flight TTS.
    speak_cached() uses a non-blocking acquire — if the service is busy
    and the current speech wasn't marked interruptible, the cue is
    silently dropped. stop() flips stop_event but only the playback loop
    checks it; if the previous speech is in the render phase (live TTS
    round-trip, 2-5s), the lock won't free until render + short play
    break finish. Retry with backoff so the cue lands as soon as the
    lock releases. ~6s total cap covers a worst-case fresh render before
    giving up silently."""
    text = _phrase(PHRASE_LISTENING)
    state.tts_service.stop()
    for delay in (0.15, 0.4, 0.8, 1.6, 3.0):
        time.sleep(delay)
        if state.tts_service.speak_cached(text):
            return
    logger.warning("listening cue dropped: TTS busy after retries")


def _tts_available() -> bool:
    return bool(
        state.tts_service
        and state.tts_service.available
        and not state._speaker_muted
    )


def _wake_if_sleepy(source: str):
    """If Lamp is currently sleeping, fire a stretching wake emotion so a
    click pulls her out of sleep before the listening cue lands. Calls
    the /emotion handler in-process — it clears `_sleeping`, cancels the
    sleepy auto-release timer, plays the wake animation, and auto-deactivates
    any active scene (e.g. Night mode)."""
    if not state._sleeping:
        return
    logger.info("%s single click -- waking from sleep", source)
    try:
        from lelamp.models import EmotionRequest
        from lelamp.routes.emotion import express_emotion
        express_emotion(EmotionRequest(emotion="stretching"))
    except Exception as e:
        logger.warning("Wake emotion call failed: %s", e)


def single_click_action(source: str = "button"):
    """Stop in-flight speech / unmute mic, then announce listening cue."""
    from lelamp.routes.music import audio_stop
    from lelamp.routes.voice import stop_tts, unmute_mic

    _wake_if_sleepy(source)

    if state._mic_muted:
        logger.info("%s single click -- unmuting mic", source)
        unmute_mic()
    else:
        logger.info("%s single click -- stopping speaker", source)
        stop_tts()
        audio_stop()
    # Always announce the listening cue so the user hears confirmation
    # of the click — both for unmute (mic just opened) and for
    # stop-speaker (Lamp was talking, user wants the floor). The cue
    # itself preempts in-flight TTS via stop() + speak_cached retry,
    # so calling stop_tts() above is fine — _announce_listening handles
    # the lock handoff.
    if _tts_available():
        threading.Thread(
            target=_announce_listening,
            daemon=True,
            name=f"{source}-single-click-tts",
        ).start()


def triple_click_action(source: str = "button"):
    """Announce + reboot OS."""
    logger.info("%s triple click -- rebooting OS", source)
    logger.info("%s LED: amber pulse (reboot armed)", source)
    if _tts_available():
        state.tts_service.speak_cached(_phrase(PHRASE_REBOOT))
        # speak_cached is async; reboot kicks the OS before audio plays
        # without this. ~5s covers the cached "Rebooting now" clip
        # (matches long_press_action shutdown delay).
        time.sleep(5)
    subprocess.Popen(
        ["sudo", "reboot"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def head_pat_action(source: str = "touch"):
    """Speak a random pet response. Non-interrupting: if TTS is busy
    (Lamp already talking), drop silently so petting mid-speech doesn't
    truncate her sentence. After the phrase actually plays, ping Lamp Go
    so the agent records the petting moment (silent — NO_REPLY)."""
    text = _random_head_pat_phrase()
    logger.info("%s head pat -- %r", source, text)
    if not _tts_available() or not text:
        return

    def _speak_then_notify():
        if state.tts_service.speak_cached(text):
            _notify_head_pat(text)

    threading.Thread(
        target=_speak_then_notify,
        daemon=True,
        name=f"{source}-head-pat-tts",
    ).start()


def long_press_action(source: str = "button"):
    """Announce, park servos, then shutdown OS."""
    logger.info("%s long press -- shutting down OS", source)
    logger.info("%s LED: red solid (shutdown armed)", source)

    # Step 1: TTS announce.
    if _tts_available():
        state.tts_service.speak_cached(_phrase(PHRASE_SHUTDOWN))
        time.sleep(5)

    # Step 2: park servo in safe pose then cut torque, otherwise the
    # body slams down when systemd kills the process mid-pose.
    try:
        from lelamp.routes.servo import release_servos

        logger.info("%s long press -- releasing servo before shutdown", source)
        release_servos()
    except Exception as e:
        logger.warning(f"Servo release before shutdown failed: {e}")

    # Step 3: shutdown OS.
    subprocess.Popen(
        ["sudo", "shutdown", "-h", "now"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _factory_reset_phrase() -> str:
    """Inline i18n until PHRASE_FACTORY_RESET lands in i18n.py."""
    lang = _current_lang()
    if lang.startswith("vi"):
        return "Đang khôi phục cài đặt gốc. Lamp sẽ khởi động lại."
    if lang.startswith("zh"):
        return "正在恢复出厂设置，Lamp 将重新启动。"
    return "Factory reset starting. Lamp will reboot."


def factory_reset_action(source: str = "button"):
    """Announce + POST /api/system/factory-reset on lamp-server. Lamp-server
    wipes per-device state (config, API keys, enrollments, WiFi) and reboots
    into AP setup mode. Lelamp does NOT touch state itself — single source of
    truth for what gets wiped lives in lamp-server's factoryResetWipePaths.

    Authoritative because of physical presence: 10s deliberate hold + the
    /api/system/factory-reset endpoint allows loopback origin without Bearer
    (see lamp server.go adminOrLoopbackAuth)."""
    logger.info("%s factory-reset hold (10s+) -- triggering soft reset", source)
    logger.info("%s LED: red solid (factory-reset armed)", source)

    # Step 1: TTS announce so the user knows the gesture registered. Brief —
    # the reboot lands ~5s after lamp-server accepts the POST, we want the
    # announce + 3s settle window to fit inside that.
    if _tts_available():
        state.tts_service.speak_cached(_factory_reset_phrase())
        time.sleep(3)

    # Step 2: park servo before reboot, same reasoning as long_press_action —
    # systemd will kill us mid-pose otherwise and the body slams.
    try:
        from lelamp.routes.servo import release_servos

        release_servos()
    except Exception as e:
        logger.warning(f"Servo release before factory-reset failed: {e}")

    # Step 3: trigger the Go-side wipe. Loopback bypasses admin auth (see
    # lamp server.go adminOrLoopbackAuth) so this works even on devices that
    # never completed setup (no llm_api_key in config).
    try:
        requests.post(
            "http://127.0.0.1:5000/api/system/factory-reset",
            json={},
            timeout=3.0,
        )
    except Exception as e:
        logger.error("factory-reset HTTP call failed: %s", e)
