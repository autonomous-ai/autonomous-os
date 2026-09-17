"""Persistent mode feedback yields to higher-priority visual ownership."""
from unittest.mock import Mock
import pytest
from hal.drivers.harness import led as led


@pytest.fixture
def lighting(monkeypatch):
    monkeypatch.setattr(led, '_enabled', False)
    monkeypatch.setattr(led, '_capturing', False)
    for flag in ('_sleeping', '_tts_speaking', '_music_playing', '_thinking_cue_active'):
        monkeypatch.setattr(led.state, flag, False)
    monkeypatch.setattr(led.state, 'rgb_service', Mock())
    monkeypatch.setattr(led.state, '_effect_thread', None)
    monkeypatch.setattr(led.state, '_mic_muted_led_owns_strip', lambda: False)
    monkeypatch.setattr(led.state, '_start_preset_effect', Mock())
    monkeypatch.setattr(led.state, '_stop_current_effect', Mock())
    monkeypatch.setattr(led.state, '_restore_user_led', Mock())


def test_on_uses_live_preset_and_off_restores_without_saving(lighting, monkeypatch):
    saved = {'type': 'solid', 'color': [8, 4, 1]}
    monkeypatch.setattr(led.state, '_user_led_state', saved)
    led.set_enabled(True)
    assert led.restore()
    led.state._start_preset_effect.assert_called_once_with(led.BUTTON_LED_PRESETS['harness_on'], 'led-harness-mode')
    thread = Mock()
    thread.name = 'led-harness-mode'
    monkeypatch.setattr(led.state, '_effect_thread', thread)
    assert led.restore()
    led.state._start_preset_effect.assert_called_once()
    led.set_enabled(False)
    led.state._stop_current_effect.assert_called_once()
    led.state.rgb_service.clear.assert_called_once()
    assert led.state._user_led_state is saved
    assert not led.restore()


@pytest.mark.parametrize('flag', ['_sleeping', '_tts_speaking', '_music_playing', '_thinking_cue_active', 'privacy'])
def test_priority_then_restore(lighting, monkeypatch, flag):
    led.set_enabled(True)
    if flag == 'privacy':
        monkeypatch.setattr(led.state, '_mic_muted_led_owns_strip', lambda: True)
    else:
        monkeypatch.setattr(led.state, flag, True)
    assert not led.restore()
    led.state._start_preset_effect.assert_not_called()
    if flag == 'privacy':
        monkeypatch.setattr(led.state, '_mic_muted_led_owns_strip', lambda: False)
    else:
        monkeypatch.setattr(led.state, flag, False)
    assert led.restore()


def test_no_led_is_noop(lighting, monkeypatch):
    monkeypatch.setattr(led.state, 'rgb_service', None)
    led.set_enabled(True)
    assert not led.restore()
    led.set_enabled(False)
    led.state._start_preset_effect.assert_not_called()
    led.state._restore_user_led.assert_not_called()


def test_ambient_cannot_replace_or_stop_mode_indicator(lighting, monkeypatch):
    from hal.routes import led as routes
    from hal.models import LEDEffectRequest
    monkeypatch.setattr(led, '_enabled', True)
    thread = Mock()
    thread.name = 'led-harness-mode'
    monkeypatch.setattr(led.state, '_effect_thread', thread)
    routes.start_led_effect(LEDEffectRequest(effect='breathing', color=[10, 10, 10], transient=True))
    routes.stop_led_effect()
    led.state._stop_current_effect.assert_not_called()


def test_route_restore_uses_common_priority_policy(lighting, monkeypatch):
    from hal.routes import led as routes
    monkeypatch.setattr(led, '_enabled', True)
    routes.restore_led()
    led.state._restore_user_led.assert_called_once()


def test_optional_led_failure_does_not_break_gestures(lighting):
    led.state._restore_user_led.side_effect = OSError('LED unavailable')
    led.set_enabled(True)
    assert led.enabled()


def test_capture_uses_listening_preset_and_restores_mode(lighting, monkeypatch):
    led.set_enabled(True)
    led.set_capturing(True)
    assert led.restore()
    led.state._start_preset_effect.assert_called_with(
        led.EMOTION_PRESETS[led.EMO_LISTENING], 'led-harness-capture')
    thread = Mock()
    thread.name = 'led-harness-capture'
    monkeypatch.setattr(led.state, '_effect_thread', thread)
    assert led.owns_effect()
    led.set_capturing(False)
    assert led.restore()
    led.state._start_preset_effect.assert_called_with(
        led.BUTTON_LED_PRESETS['harness_on'], 'led-harness-mode')


def test_disable_clears_capture_and_ignores_late_ready(lighting):
    led.set_enabled(True)
    led.set_capturing(True)
    led.set_enabled(False)
    led.set_capturing(True)
    assert not led._capturing
    assert not led.restore()
