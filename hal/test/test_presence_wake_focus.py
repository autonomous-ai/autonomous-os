"""Tests for opening the voice follow-up window when a person enters."""

import os
from unittest import mock

# The face-perception package creates these directories during module import.
# Keep this focused unit test runnable on a development host, where /root is
# normally read-only, without changing an explicitly supplied test location.
os.environ.setdefault("HAL_USERS_DIR", "/tmp/autonomous-hal-test-users")
os.environ.setdefault("HAL_STRANGERS_DIR", "/tmp/autonomous-hal-test-strangers")

import hal.app_state as state
from hal.drivers.sensing.sensing_service import SensingService


def test_recognized_presence_enter_grants_the_existing_wakeword_focus_window(monkeypatch):
    voice = mock.Mock()
    monkeypatch.setattr(state, "voice_service", voice)

    SensingService._grant_wakeword_focus_for_presence(
        "Person detected — 1 face(s) visible (friend (leo))"
    )

    voice.grant_wakeword_focus.assert_called_once_with("presence.enter")


def test_stranger_only_presence_enter_does_not_grant_wakeword_focus(monkeypatch):
    voice = mock.Mock()
    monkeypatch.setattr(state, "voice_service", voice)

    SensingService._grant_wakeword_focus_for_presence(
        "Person detected — 1 face(s) visible (stranger (stranger_1))"
    )

    voice.grant_wakeword_focus.assert_not_called()


def test_presence_enter_without_voice_pipeline_is_a_noop(monkeypatch):
    monkeypatch.setattr(state, "voice_service", None)

    SensingService._grant_wakeword_focus_for_presence(
        "Person detected — 1 face(s) visible (friend (leo))"
    )
