"""Regression tests for sleepy owning the LED strip.

Sleep teardown is concurrent with TTS/music completion and pending emotion
restores. None of those late callbacks may revive the mic-muted red indicator
or the music idle fallback after sleepy has cleared the strip.
"""
import threading
import unittest
from unittest import mock

import hal.app_state as state
from hal.presets import EMO_SLEEPY


class _FakeRGB:
    def __init__(self):
        self.clear_calls = 0
        self.dispatches = []

    def clear(self):
        self.clear_calls += 1

    def dispatch(self, command, value):
        self.dispatches.append((command, value))


class _PendingRestore:
    def __init__(self):
        self.cancelled = False

    def is_alive(self):
        return True

    def cancel(self):
        self.cancelled = True


class TestSleepyLED(unittest.TestCase):
    _STATE_FIELDS = (
        "rgb_service", "animation_service", "voice_service", "tts_service", "music_service",
        "_sleeping", "_current_emotion", "_mic_muted", "_speaker_muted",
        "_sleepy_auto_muted_mic", "_sleepy_auto_muted_speaker", "_mic_muted_led",
        "_restore_timer", "_effect_thread", "_effect_stop", "_effect_name",
        "_effect_base_color", "_user_led_state", "_tts_speaking", "_music_playing",
    )

    def setUp(self):
        self.saved = {name: getattr(state, name) for name in self._STATE_FIELDS}
        state.rgb_service = _FakeRGB()
        state.animation_service = None
        state.voice_service = None
        state.tts_service = None
        state.music_service = None
        state._sleeping = True
        state._current_emotion = EMO_SLEEPY
        state._mic_muted = False
        state._speaker_muted = False
        state._sleepy_auto_muted_mic = False
        state._sleepy_auto_muted_speaker = False
        state._mic_muted_led = False
        state._restore_timer = None
        state._effect_thread = None
        state._effect_stop = threading.Event()
        state._effect_name = None
        state._effect_base_color = None
        state._user_led_state = {"type": "solid", "color": [20, 30, 40]}
        state._tts_speaking = False
        state._music_playing = False

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(state, name, value)

    def test_sleepy_cancels_pending_restore_and_blocks_muted_red_repaint(self):
        pending = _PendingRestore()
        state._restore_timer = pending

        state._finalize_sleepy_peripherals(mute_mic=True, mute_speaker=True)

        self.assertTrue(pending.cancelled)
        self.assertIsNone(state._restore_timer)
        self.assertTrue(state._mic_muted)
        self.assertEqual(state.rgb_service.clear_calls, 1)

        # A late TTS/music/emotion restore must leave the strip dark, even if
        # the mic-muted indicator was already active before sleepy.
        state._mic_muted_led = True
        state._restore_user_led()
        state._start_mic_muted_effect()

        self.assertEqual(state.rgb_service.clear_calls, 2)
        self.assertEqual(state.rgb_service.dispatches, [])

    def test_sleepy_blocks_late_tts_and_music_wave_starts(self):
        state._on_tts_speak_start()
        state._on_music_play_start()

        self.assertFalse(state._tts_speaking)
        self.assertFalse(state._music_playing)
        self.assertEqual(state.rgb_service.dispatches, [])

    def test_muted_indicator_can_resume_after_wake(self):
        state._sleeping = False
        state._mic_muted_led = True

        with mock.patch.object(state, "_start_preset_effect") as start_effect:
            state._start_mic_muted_effect()

        start_effect.assert_called_once_with(state.STATUS_LED_PRESETS["mic_muted"], "led-mic-muted")

if __name__ == "__main__":
    unittest.main()
