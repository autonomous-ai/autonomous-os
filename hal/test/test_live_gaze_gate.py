"""The armed gaze gate must reject live entry before any realtime preparation."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hal import config
from hal.drivers.voice.voice_service import VoiceService


@pytest.mark.parametrize("wake,gaze,shadow,focus,expected", [
    (True, True, False, False, "skip"),
    (True, True, False, True, "live"),
    (True, True, True, False, "live"),
    (True, False, False, False, "live"),
    (False, True, False, False, "live"),
])
def test_live_entry_respects_gaze_focus_and_configuration(monkeypatch, wake, gaze, shadow, focus, expected):
    monkeypatch.setattr(config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(config, "WAKEWORD_ENABLED", wake)
    monkeypatch.setattr(config, "GAZE_WAKE_ENABLED", gaze)
    monkeypatch.setattr(config, "GAZE_WAKE_SHADOW", shadow)
    service = object.__new__(VoiceService)
    service._music_is_playing = lambda: False
    service._wakeword_focus = SimpleNamespace(is_active=lambda: focus)
    service._realtime = Mock()
    service._realtime.wait_until_available.return_value = True

    assert service._live_decision([]) == expected
    if expected == "skip":
        service._realtime.prepare_turn.assert_not_called()
        service._realtime.wait_until_available.assert_not_called()
    else:
        service._realtime.prepare_turn.assert_called_once()


def test_expired_focus_closes_next_live_entry(monkeypatch):
    monkeypatch.setattr(config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(config, "WAKEWORD_ENABLED", True)
    monkeypatch.setattr(config, "GAZE_WAKE_ENABLED", True)
    monkeypatch.setattr(config, "GAZE_WAKE_SHADOW", False)
    service = object.__new__(VoiceService)
    service._music_is_playing = lambda: False
    service._wakeword_focus = Mock()
    service._wakeword_focus.is_active.side_effect = [True, False]
    service._realtime = Mock()
    service._realtime.wait_until_available.return_value = True
    assert service._live_decision([]) == "live"
    assert service._live_decision([]) == "skip"
    service._realtime.prepare_turn.assert_called_once()
