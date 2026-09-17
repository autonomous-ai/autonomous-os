"""Resting Harness indicator, subordinate to sleep, privacy and active voice."""

import logging

import hal.app_state as state
from hal.presets import BUTTON_LED_PRESETS, EMOTION_PRESETS, EMO_LISTENING

logger = logging.getLogger(__name__)

_enabled = False
_capturing = False
_THREAD_NAME = "led-harness-mode"
_CAPTURE_THREAD_NAME = "led-harness-capture"


def owns_effect():
    return state._effect_thread is not None and state._effect_thread.name in (_THREAD_NAME, _CAPTURE_THREAD_NAME)


def enabled():
    return _enabled


def restore():
    """Paint the mode indicator without changing the saved user LED preference."""
    if not _enabled or not state.rgb_service:
        return False
    if (state._sleeping or state._tts_speaking or state._music_playing
            or state._thinking_cue_active or state._mic_muted_led_owns_strip()):
        return False
    name = _CAPTURE_THREAD_NAME if _capturing else _THREAD_NAME
    preset = EMOTION_PRESETS[EMO_LISTENING] if _capturing else BUTTON_LED_PRESETS["harness_on"]
    if (not owns_effect() or state._effect_thread.name != name
            or not state._effect_thread.is_alive()):
        state._start_preset_effect(preset, name)
    return True


def set_enabled(value):
    global _enabled, _capturing
    value = bool(value)
    if _enabled == value:
        return
    _enabled = value
    if not value:
        _capturing = False
    if not state.rgb_service:
        return
    try:
        if not value and owns_effect():
            state._stop_current_effect()
            state.rgb_service.clear()
        state._restore_user_led()
    except Exception:
        logger.warning("Harness LED feedback unavailable", exc_info=True)


def set_capturing(value):
    """Show microphone readiness using the live listening preset, LED only."""
    global _capturing
    value = bool(value) and _enabled
    if _capturing == value:
        return
    _capturing = value
    if not state.rgb_service:
        return
    try:
        state._restore_user_led()
    except Exception:
        logger.warning("Harness capture LED unavailable", exc_info=True)
