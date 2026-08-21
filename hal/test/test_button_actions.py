"""Focused tests for shared physical-button actions."""

from unittest import mock

import hal.app_state as state
from hal.drivers import button_actions
from hal.routes import music as music_routes


class _FakeTracker:
    def __init__(self, is_tracking: bool):
        self.is_tracking = is_tracking
        self.stop = mock.Mock()


def test_single_click_stops_active_tracking_even_with_hardware_mic_muted():
    tracker = _FakeTracker(is_tracking=True)
    with (
        mock.patch.object(state, "tracker_service", tracker),
        mock.patch.object(state, "_hw_mic_switch_muted", True),
    ):
        button_actions.single_click_action("test")

    tracker.stop.assert_called_once_with()


def test_single_click_does_not_stop_an_inactive_tracker():
    tracker = _FakeTracker(is_tracking=False)
    with (
        mock.patch.object(state, "tracker_service", tracker),
        mock.patch.object(state, "_hw_mic_switch_muted", True),
    ):
        button_actions.single_click_action("test")

    tracker.stop.assert_not_called()


class _FakeMusic:
    """Minimal MusicService stand-in: playing + stop, nothing else."""

    def __init__(self, playing: bool = True):
        self.available = True
        self.playing = playing
        self.stop = mock.Mock()


def _quiet_single_click(**state_overrides):
    """Run single_click_action with the network / speaker side effects stubbed."""
    patches = [
        mock.patch.object(button_actions, "_cancel_agent_speech"),
        mock.patch.object(button_actions, "_wake_if_sleepy"),
        mock.patch.object(button_actions, "_grant_wakeword_focus"),
        mock.patch.object(button_actions, "play_ack_chime"),
        mock.patch.object(button_actions, "announce_listening_cue"),
        mock.patch("hal.routes.voice.stop_tts"),
        mock.patch("hal.routes.voice.unmute_mic"),
        mock.patch("hal.routes.music.unmute_speaker"),
        mock.patch.object(state, "tracker_service", None),
        mock.patch.object(state, "_hw_mic_switch_muted", None),
    ]
    for key, value in state_overrides.items():
        patches.append(mock.patch.object(state, key, value))
    state._music_cancel_ms = 0.0
    for p in patches:
        p.start()
    try:
        button_actions.single_click_action("test")
    finally:
        for p in reversed(patches):
            p.stop()


def test_single_click_stops_music_even_when_mic_is_muted():
    """The mic-muted branch used to unmute the mic and leave music playing."""
    musicsvc = _FakeMusic(playing=True)
    try:
        _quiet_single_click(music_service=musicsvc, _mic_muted=True, _speaker_muted=False)
        musicsvc.stop.assert_called_once_with()
    finally:
        state._music_cancel_ms = 0.0


def test_single_click_stops_music_on_the_stop_speaker_branch():
    musicsvc = _FakeMusic(playing=True)
    try:
        _quiet_single_click(music_service=musicsvc, _mic_muted=False, _speaker_muted=False)
        musicsvc.stop.assert_called_once_with()
    finally:
        state._music_cancel_ms = 0.0


def test_single_click_arms_the_music_cancel_watermark():
    """A cancelled turn's pending play tool call must be refused, not played."""
    from hal.models import MusicPlayRequest

    musicsvc = _FakeMusic(playing=False)
    try:
        _quiet_single_click(music_service=musicsvc, _mic_muted=False, _speaker_muted=False)
        assert state.music_cancel_active() is True

        with (
            mock.patch.object(state, "music_service", musicsvc),
            mock.patch.object(state, "_speaker_muted", False),
        ):
            result = music_routes.audio_play(MusicPlayRequest(query="some song"))
        assert result == {"status": "suppressed"}
    finally:
        state._music_cancel_ms = 0.0


def test_music_cancel_watermark_expires():
    import time

    try:
        state._music_cancel_ms = time.monotonic() - (state.MUSIC_CANCEL_GUARD_S + 1.0)
        assert state.music_cancel_active() is False
    finally:
        state._music_cancel_ms = 0.0
