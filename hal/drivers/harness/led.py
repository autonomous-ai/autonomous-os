"""Resting Harness indicator, subordinate to sleep, privacy and active voice."""

import logging

import hal.app_state as state
from hal.presets import BUTTON_LED_PRESETS

logger = logging.getLogger(__name__)

_enabled = False
_THREAD_NAME = "led-harness-mode"


def owns_effect():
    return state._effect_thread is not None and state._effect_thread.name == _THREAD_NAME


def enabled():
    return _enabled


def restore():
    """Paint the mode indicator without changing the saved user LED preference."""
    if not _enabled or not state.rgb_service:
        return False
    if (state._sleeping or state._tts_speaking or state._music_playing
            or state._thinking_cue_active or state._mic_muted_led_owns_strip()):
        return False
    if not owns_effect() or not state._effect_thread.is_alive():
        state._start_preset_effect(BUTTON_LED_PRESETS["harness_on"], _THREAD_NAME)
    return True


def set_enabled(value):
    global _enabled
    value = bool(value)
    if _enabled == value:
        return
    _enabled = value
    if not state.rgb_service:
        return
    try:
        if not value and owns_effect():
            state._stop_current_effect()
            state.rgb_service.clear()
        state._restore_user_led()
    except Exception:
        logger.warning("Harness LED feedback unavailable", exc_info=True)
