"""Harness ON refuses sleepy from every caller (absence timer, API, button)."""
import unittest
from unittest.mock import patch

import hal.app_state as state
from hal.models import EmotionRequest
from hal.routes import emotion


class HarnessBlocksSleepTests(unittest.TestCase):
    def test_sleepy_is_ignored_while_harness_on(self):
        state._sleeping = False
        with patch("hal.drivers.voice._internal.harness_voice.read_voice_mode",
                   return_value={"enabled": True, "generation": 1}):
            result = emotion.express_emotion(EmotionRequest(emotion="sleepy"), source="test")
        self.assertEqual(result["status"], "ignored")
        self.assertFalse(state._sleeping)

    def test_unavailable_harness_does_not_block(self):
        with patch("hal.drivers.voice._internal.harness_voice.read_voice_mode",
                   return_value={"enabled": False, "generation": -1, "unavailable": True}):
            self.assertFalse(emotion.harness_blocks_sleep())


if __name__ == "__main__":
    unittest.main()
