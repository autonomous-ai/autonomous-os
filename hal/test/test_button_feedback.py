"""Shared GPIO/MPR hold feedback, including slow RGB dispatch ordering."""

import threading
import unittest
from unittest.mock import Mock, patch

from hal.board.gpio_button import ButtonConfig
from hal.drivers import button_actions as feedback
from hal.drivers.gpio_button import GPIOButtonHandler


class HoldLEDFeedbackTests(unittest.TestCase):
    def test_blink_tiers_and_solid_reset(self):
        colors = []
        condition = threading.Condition()

        def dispatch(color, **kwargs):
            with condition:
                colors.append(color)
                condition.notify_all()

        def await_count(count):
            with condition:
                self.assertTrue(condition.wait_for(lambda: len(colors) >= count, timeout=2))

        led = feedback.HoldLEDFeedback()
        with patch.object(feedback, "dispatch_led", side_effect=dispatch), patch.object(
            feedback, "warn_color", side_effect=lambda tier: tier,
        ), patch.object(feedback, "LED_BLINK_HALF_PERIOD_S", 0.01):
            try:
                led.set_tier(1)
                await_count(2)
                self.assertEqual(colors[:2], ["sleep_warn", feedback.LED_OFF])
                led.set_tier(2)
                with condition:
                    self.assertTrue(condition.wait_for(lambda: "shutdown_warn" in colors, timeout=2))
                led.set_tier(3)
                with condition:
                    self.assertTrue(condition.wait_for(lambda: "factory_reset" in colors, timeout=2))
                led.release()
                led.commit(2)
                self.assertEqual(colors[-1], "factory_reset")
            finally:
                led.stop()
                led._thread.join(timeout=2)

    def test_release_is_nonblocking_and_commit_serializes_old_blink(self):
        entered, unblock, committed = threading.Event(), threading.Event(), threading.Event()
        colors = []

        def dispatch(color, **kwargs):
            colors.append(color)
            if len(colors) == 1:
                entered.set()
                self.assertTrue(unblock.wait(2))

        led = feedback.HoldLEDFeedback()
        with patch.object(feedback, "dispatch_led", side_effect=dispatch), patch.object(
            feedback, "warn_color", side_effect=lambda tier: tier,
        ):
            try:
                led.set_tier(1)
                self.assertTrue(entered.wait(2))
                led.release()
                action = threading.Thread(target=lambda: (led.commit(5), committed.set()))
                action.start()
                self.assertFalse(committed.is_set())
                unblock.set()
                self.assertTrue(committed.wait(2))
                action.join(2)
                self.assertEqual(colors, ["sleep_warn", "shutdown_warn"])
                led.commit(10)
                self.assertEqual(colors[-1], "factory_reset")
            finally:
                unblock.set()
                led.stop()
                led._thread.join(2)

    def test_stop_cancels_future_blinks_without_waiting_for_rgb(self):
        entered, unblock = threading.Event(), threading.Event()
        led = feedback.HoldLEDFeedback()
        with patch.object(feedback, "dispatch_led", side_effect=lambda _, **kwargs: (entered.set(), unblock.wait(2))) as dispatch, patch.object(feedback, "warn_color", return_value=(1, 2, 3)):
            led.set_tier(1)
            self.assertTrue(entered.wait(2))
            led.stop()
            unblock.set()
            led._thread.join(2)
            self.assertFalse(led._thread.is_alive())
            self.assertEqual(dispatch.call_count, 1)
            led.commit(10)
            self.assertEqual(dispatch.call_count, 1)

    def test_stop_during_effect_cleanup_prevents_stale_rgb_write(self):
        import hal.app_state as state

        entered, unblock = threading.Event(), threading.Event()
        led = feedback.HoldLEDFeedback()
        rgb = Mock()
        result = []
        with patch.object(state, "rgb_service", rgb), patch.object(
            state, "_stop_current_effect", side_effect=lambda: (entered.set(), unblock.wait(2)),
        ):
            action = threading.Thread(target=lambda: result.append(led.commit(5)))
            action.start()
            self.assertTrue(entered.wait(2))
            led.stop()
            action.join(2)
            self.assertEqual(result, [False])
            unblock.set()
            led._thread.join(2)
            rgb.dispatch.assert_not_called()

    def test_superseded_commit_waits_for_inflight_rgb_then_cancels(self):
        entered, unblock, committing = threading.Event(), threading.Event(), threading.Event()
        led = feedback.HoldLEDFeedback()
        result = []
        with patch.object(feedback, "dispatch_led", side_effect=lambda *args, **kwargs: (entered.set(), unblock.wait(2))):
            led.set_tier(1)
            self.assertTrue(entered.wait(2))

            def commit():
                committing.set()
                result.append(led.commit(5))

            action = threading.Thread(target=commit)
            action.start()
            self.assertTrue(committing.wait(2))
            with led._condition:
                self.assertTrue(led._condition.wait_for(lambda: led._committing, timeout=2))
            led.release()
            self.assertEqual(result, [])
            unblock.set()
            action.join(2)
            self.assertEqual(result, [False])
            led.stop()
            led._thread.join(2)

    def test_rgb_error_does_not_block_action(self):
        led = feedback.HoldLEDFeedback()
        with patch.object(feedback, "warn_color", side_effect=RuntimeError("no LED")):
            led.commit(5)
            led.stop()
            led._thread.join(2)
            self.assertFalse(led._thread.is_alive())

    def test_presets_are_read_at_dispatch_time(self):
        from hal.presets import BUTTON_LED_PRESETS

        with patch.dict(BUTTON_LED_PRESETS, sleep_warn={"color": [1, 2, 3]}):
            self.assertEqual(feedback.warn_color("sleep_warn"), (1, 2, 3))
            BUTTON_LED_PRESETS["sleep_warn"] = {"color": [4, 5, 6]}
            self.assertEqual(feedback.warn_color("sleep_warn"), (4, 5, 6))

    def test_dispatch_uses_high_priority_and_stops_effect(self):
        import hal.app_state as state
        from hal.drivers.base import Priority
        from hal.presets import RGB_CMD_SOLID

        rgb = Mock()
        with patch.object(state, "rgb_service", rgb), patch.object(state, "_stop_current_effect") as stop:
            feedback.dispatch_led((1, 2, 3))
        stop.assert_called_once_with()
        rgb.dispatch.assert_called_once_with(RGB_CMD_SOLID, (1, 2, 3), priority=Priority.HIGH)

    def test_gpio_reports_tiers_and_shared_commit_precedes_action(self):
        handler = GPIOButtonHandler(ButtonConfig(chip=0, line=100, debounce_ns=0))
        handler._hold_led = Mock()
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.side_effect = [False, False, True]
        handler._hold_watcher_stop = stop
        handler._press_start = 0
        with patch("hal.drivers.gpio_button.time.monotonic", side_effect=[2, 5, 10]):
            handler._hold_watcher(stop)
        self.assertEqual([c.args[0] for c in handler._hold_led.set_tier.call_args_list], [1, 2, 3])
        order = Mock()
        order.attach_mock(handler._hold_led.commit, "commit")
        with patch("hal.drivers.gpio_button.hold_release_action") as action:
            order.attach_mock(action, "action")
            handler._run_hold_action(5)
        self.assertEqual([c[0] for c in order.mock_calls], ["commit", "action"])
        handler._hold_led.commit.assert_called_once_with(5)
        action.assert_called_once_with(5, source="GPIO button")

    def test_gpio_old_watcher_cannot_override_new_hold(self):
        handler = GPIOButtonHandler(ButtonConfig(chip=0, line=100, debounce_ns=0))
        handler._hold_led = Mock()
        handler._hold_watcher_stop = threading.Event()
        handler._hold_watcher(threading.Event())
        handler._hold_led.set_tier.assert_not_called()


if __name__ == "__main__":
    unittest.main()
