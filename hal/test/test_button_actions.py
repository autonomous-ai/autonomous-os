"""Focused tests for shared physical-button actions."""

from unittest import mock

import hal.app_state as state
from hal.drivers import button_actions


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
