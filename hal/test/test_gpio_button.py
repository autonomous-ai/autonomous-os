"""GPIO gesture and lifecycle tests without hardware or destructive actions."""
from unittest import mock

import pytest

from hal.board.gpio_button import ButtonConfig
from hal.drivers import button_actions, gpio_button


class ImmediateThread:
    def __init__(self, target, args=(), kwargs=None, **unused):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        if self.target.__name__ != "_hold_watcher":
            self.target(*self.args, **self.kwargs)


@pytest.fixture
def actions():
    with (
        mock.patch.object(gpio_button, "HoldLEDFeedback") as led,
        mock.patch.object(gpio_button.threading, "Thread", ImmediateThread),
        mock.patch.object(button_actions, "factory_reset_action") as reset,
        mock.patch.object(button_actions, "hold_release_action") as normal,
        mock.patch.object(gpio_button, "single_click_action") as single,
        mock.patch.object(gpio_button, "triple_click_action") as triple,
        mock.patch.object(gpio_button, "announce_listening_cue") as cue,
    ):
        yield led, reset, normal, single, triple, cue


def handler(**kwargs):
    return gpio_button.GPIOButtonHandler(ButtonConfig(chip=0, line=99, debounce_ns=0),
                                         name="reset", behavior="factory_reset", **kwargs)


def edge(button, level, seconds):
    with mock.patch.object(gpio_button.time, "monotonic", return_value=seconds):
        button._on_edge(0, 99, level, int((seconds + 1) * 1e9))


@pytest.mark.parametrize("duration,expected", [(0.1, 0), (4.999, 0), (5.0, 1), (20.0, 1)])
def test_reset_release_boundary(actions, duration, expected):
    led, reset, normal, single, triple, cue = actions
    button = handler()
    edge(button, 0, 0)
    reset.assert_not_called()
    edge(button, 1, duration)
    assert reset.call_count == expected
    if expected:
        reset.assert_called_once_with("GPIO button reset (gpiochip0/line99)")
        led.return_value.commit_tier.assert_called_once_with(3)
        led.return_value.commit.assert_not_called()
    edge(button, 1, duration + 1)
    assert reset.call_count == expected
    for action in (normal, single, triple, cue):
        action.assert_not_called()


def test_reset_ignores_triple_tap_and_watchdog(actions):
    button = handler()
    for start in (1, 2, 3):
        edge(button, 0, start)
        edge(button, 2, start + 0.01)
        assert button._pressed
        edge(button, 1, start + 0.1)
    button._on_click_timeout()
    for action in actions[1:]:
        action.assert_not_called()
    assert button._click_timer is None


def test_reset_hold_feedback_at_threshold_without_action(actions):
    button = handler()
    edge(button, 0, 0)
    stop = button._hold_watcher_stop
    with mock.patch.object(gpio_button.time, "monotonic", return_value=5):
        with mock.patch.object(stop, "wait", return_value=True):
            button._hold_watcher(stop)
    actions[0].return_value.set_tier.assert_called_once_with(3)
    actions[1].assert_not_called()


def test_independent_buttons_use_independent_release_duration(actions):
    first, second = handler(), handler(hold_s=8)
    edge(first, 0, 0)
    edge(second, 0, 4)
    edge(first, 1, 5)
    edge(second, 1, 9)
    assert actions[1].call_count == 1


def test_stop_cancels_all_resources_and_pending_actions(actions):
    button = handler()
    edge(button, 0, 0)
    watcher = button._hold_watcher_stop
    callback, timer, lgpio = mock.Mock(), mock.Mock(), mock.Mock()
    button._callback, button._click_timer = callback, timer
    button._handle, button._lgpio = 12, lgpio
    button.stop()
    button.stop()
    assert watcher.is_set()
    callback.cancel.assert_called_once_with()
    timer.cancel.assert_called_once_with()
    lgpio.gpiochip_close.assert_called_once_with(12)
    edge(button, 1, 10)
    button._run_hold_action(10)
    button._run_single_click()
    button._on_click_timeout()
    for action in actions[1:]:
        action.assert_not_called()


def test_start_failure_closes_open_chip(actions):
    lgpio = mock.Mock()
    lgpio.gpiochip_open.return_value = 7
    lgpio.gpio_claim_alert.side_effect = RuntimeError("line busy")
    button = handler()
    with mock.patch.dict("sys.modules", lgpio=lgpio):
        with pytest.raises(RuntimeError, match="line busy"):
            button.start()
    lgpio.gpiochip_close.assert_called_once_with(7)
    assert button._stopped


def test_standard_policy_preserves_existing_hold_action(actions):
    button = gpio_button.GPIOButtonHandler(ButtonConfig(chip=0, line=100, debounce_ns=0))
    edge(button, 0, 0)
    edge(button, 1, 5)
    actions[0].return_value.commit.assert_called_once_with(5)
    actions[2].assert_called_once_with(5, source="GPIO button")
    actions[1].assert_not_called()


def test_cancelled_led_commit_does_not_reset(actions):
    actions[0].return_value.commit_tier.return_value = False
    button = handler()
    edge(button, 0, 0)
    edge(button, 1, 5)
    actions[1].assert_not_called()


def test_cleanup_errors_do_not_prevent_remaining_cleanup(actions):
    button = handler()
    button._callback = mock.Mock()
    button._callback.cancel.side_effect = RuntimeError("callback failure")
    button._lgpio = mock.Mock()
    button._lgpio.gpiochip_close.side_effect = RuntimeError("close failure")
    button._handle = 7
    button.stop()
    button._lgpio.gpiochip_close.assert_called_once_with(7)
    assert button._callback is None and button._handle is None
