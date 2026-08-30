"""The gaze acknowledgement must stay visual-only until STT has text."""

from unittest import mock

import hal.app_state as state
from hal.presets import EMO_IDLE, EMO_LISTENING


class _Timer:
    def __init__(self, *args, **kwargs):
        self.daemon = False
        self.started = False

    def start(self):
        self.started = True


def _reset_pending_state():
    with state._listening_pending_cue_lock:
        state._listening_pending_cue_id = 0
        state._listening_pending_cue_active_id = None


def test_pending_gaze_cue_is_dim_and_does_not_claim_listening_emotion(monkeypatch):
    _reset_pending_state()
    monkeypatch.setattr(state, "_sleeping", False)
    monkeypatch.setattr(state, "_tts_speaking", False)
    monkeypatch.setattr(state, "_current_emotion", EMO_IDLE)
    apply = mock.Mock()
    monkeypatch.setattr(state, "_apply_emotion_led_display", apply)
    monkeypatch.setattr(state.threading, "Timer", _Timer)

    cue_id = state.show_listening_pending_cue()

    assert cue_id == 1
    assert state._current_emotion == EMO_IDLE
    apply.assert_called_once_with(EMO_LISTENING, intensity=0.35, force_led=True)


def test_stale_pending_cue_cannot_restore_a_newer_one(monkeypatch):
    _reset_pending_state()
    monkeypatch.setattr(state, "_sleeping", False)
    monkeypatch.setattr(state, "_tts_speaking", False)
    monkeypatch.setattr(state, "_current_emotion", EMO_IDLE)
    monkeypatch.setattr(state, "_apply_emotion_led_display", mock.Mock())
    monkeypatch.setattr(state.threading, "Timer", _Timer)
    restore = mock.Mock()
    monkeypatch.setattr(state, "_restore_user_led", restore)

    old_id = state.show_listening_pending_cue()
    new_id = state.show_listening_pending_cue()

    assert not state.clear_listening_pending_cue(old_id)
    assert state.clear_listening_pending_cue(new_id)
    restore.assert_called_once()


def test_pending_cue_never_restores_over_real_listening(monkeypatch):
    _reset_pending_state()
    monkeypatch.setattr(state, "_sleeping", False)
    monkeypatch.setattr(state, "_tts_speaking", False)
    monkeypatch.setattr(state, "_current_emotion", EMO_IDLE)
    monkeypatch.setattr(state, "_apply_emotion_led_display", mock.Mock())
    monkeypatch.setattr(state.threading, "Timer", _Timer)
    restore = mock.Mock()
    monkeypatch.setattr(state, "_restore_user_led", restore)

    cue_id = state.show_listening_pending_cue()
    monkeypatch.setattr(state, "_current_emotion", EMO_LISTENING)

    assert state.clear_listening_pending_cue(cue_id)
    restore.assert_not_called()
