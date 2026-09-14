"""Local API adapter, confirmation localization and hardware privacy policy."""

import copy
from contextlib import ExitStack
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock
import requests

from hal.drivers import harness_voice_client as client
from hal.drivers import harness_voice_action as action
from hal.i18n import localized_phrase, PHRASE_HARNESS_ON, PHRASE_HARNESS_OFF


class HarnessClientTests(unittest.TestCase):
    def request(self, payload, status=200):
        session = MagicMock()
        session.post.return_value.json.return_value = payload
        session.post.return_value.status_code = status
        with patch.object(client.requests, "Session") as factory:
            factory.return_value.__enter__.return_value = session
            result = client.request_voice_toggle("gesture-id")
        self.assertFalse(session.trust_env)
        session.post.assert_called_once_with(client.OS_HARNESS_GESTURE_URL,
                                            json={"gestureId": "gesture-id"}, timeout=15,
                                            allow_redirects=False)
        return result

    def test_success_and_false_are_authoritative(self):
        for data in ({"enabled": False}, {"enabled": True, "focusAvailable": True, "agentId": "agent"}):
            self.assertEqual(self.request({"status": 1, "data": data}), data)

    def test_failure_code_and_invalid_success(self):
        with self.assertRaises(client.HarnessGestureError) as caught:
            self.request({"status": 0, "data": {"code": "no_agents"}}, 409)
        self.assertEqual(caught.exception.code, "no_agents")
        for data in ({}, {"enabled": "false"}, {"enabled": True}):
            with self.assertRaises(client.HarnessGestureError):
                self.request({"status": 1, "data": data})

    def test_timeout_never_retries(self):
        with patch.object(client.requests, "Session") as factory:
            session = factory.return_value.__enter__.return_value
            session.post.side_effect = requests.Timeout()
            with self.assertRaises(client.HarnessGestureError):
                client.request_voice_toggle("one")
            session.post.assert_called_once()


class HarnessActionTests(unittest.TestCase):
    def test_feedback_uses_device_overlay_and_restores_after_duration(self):
        from hal.board.presets_overlay import _TABLES, apply_device_presets
        from hal.presets import BUTTON_LED_PRESETS

        with ExitStack() as stack:
            for table in _TABLES.values():
                stack.enter_context(patch.dict(table, copy.deepcopy(table)))
            apply_device_presets("lamp", str(Path(__file__).resolve().parents[2] / "robots"))
            stack.enter_context(patch.object(action.state, "rgb_service", MagicMock()))
            stack.enter_context(patch.object(action.state, "_effect_thread", None))
            restore = stack.enter_context(patch.object(action.state, "_schedule_led_restore"))
            def start_effect(request):
                action.state._effect_thread = object()
            start = stack.enter_context(patch("hal.routes.led.start_led_effect", side_effect=start_effect))
            for enabled, color in ((True, [1, 1, 3]), (False, [2, 2, 2])):
                action._show_feedback(enabled)
                request = start.call_args.args[0]
                self.assertEqual(request.color, color)
                self.assertEqual(request.effect, "pulse")
                self.assertEqual(request.duration_ms, 600)
                self.assertTrue(request.transient)
                restore.assert_called_with(0.7)
            BUTTON_LED_PRESETS["harness_on"].update(effect="blink", duration_ms=900)
            action._show_feedback(True)
            self.assertEqual(start.call_args.args[0].effect, "blink")
            restore.assert_called_with(1.0)
            restore.reset_mock()
            start.side_effect = None
            action._show_feedback(True)
            restore.assert_not_called()

    def test_all_languages_and_config_lookup(self):
        expected_off = {"en": "Device mode.", "vi": "Chế độ thiết bị.",
                        "zh-CN": "设备模式。", "zh-TW": "裝置模式。"}
        # Use the project language constants (currently ISO language IDs).
        from hal.presets import LANG_EN, LANG_VI, LANG_ZH_CN, LANG_ZH_TW
        for lang, expected in zip((LANG_EN, LANG_VI, LANG_ZH_CN, LANG_ZH_TW), expected_off.values()):
            self.assertEqual(localized_phrase(PHRASE_HARNESS_OFF, lang), expected)
            self.assertIn("Test agent", localized_phrase(PHRASE_HARNESS_ON, lang).format(agent="Test agent"))
            with patch("hal.config._os_cfg_get", return_value=lang):
                self.assertEqual(action.confirmation_phrase({"enabled": False}), expected)
                self.assertIn("Friendly", action.confirmation_phrase({"enabled": True, "agentId": "id", "agentName": "Friendly"}))
                self.assertTrue(action.failure_phrase("no_agents"))
                self.assertTrue(action.failure_phrase("harness_offline"))
                self.assertTrue(action.failure_phrase("busy"))

    def test_privacy_switch_prevents_api_and_ack(self):
        with patch.object(action.state, "_hw_mic_switch_muted", True), patch.object(action, "request_voice_toggle") as request:
            action.toggle_harness_voice()
            request.assert_not_called()

    def test_action_unique_ids_and_actual_result_ack(self):
        with patch.object(action.state, "_hw_mic_switch_muted", False), \
                patch.object(action, "request_voice_toggle", return_value={"enabled": False}) as request, \
                patch.object(action, "_show_feedback") as led, \
                patch("hal.drivers.button_actions._speak_gesture_ack") as speak:
            action.toggle_harness_voice()
            action.toggle_harness_voice()
            self.assertNotEqual(request.call_args_list[0].args, request.call_args_list[1].args)
            led.assert_called_with(False)
            speak.assert_called_with(localized_phrase(PHRASE_HARNESS_OFF), "MPR121")

    def test_unknown_outcome_never_announces_success(self):
        with patch.object(action.state, "_hw_mic_switch_muted", False), \
                patch.object(action, "request_voice_toggle", side_effect=client.HarnessGestureError("unknown")), \
                patch.object(action, "_show_feedback") as led, \
                patch("hal.drivers.button_actions._speak_gesture_ack") as speak:
            action.toggle_harness_voice()
            led.assert_not_called()
            speak.assert_called_once_with(action.failure_phrase("unknown"), "MPR121")
