"""Tests for the consolidated board platform layer. Pure logic — no hardware."""
import unittest

from lelamp.platform.board import (
    DEFAULT_BOARD_ID,
    PROFILES,
    board_profile,
    detect_board_id,
    read_device_tree_model,
)


class TestDetectBoardId(unittest.TestCase):
    def test_pi5(self):
        self.assertEqual(detect_board_id("Raspberry Pi 5 Model B Rev 1.0".lower()), "raspberry_pi_5")

    def test_orangepi_sun60(self):
        self.assertEqual(detect_board_id("opi sun60iw2 board"), "orangepi_sun60")

    def test_pi4_falls_through_to_default(self):
        self.assertEqual(detect_board_id("raspberry pi 4 model b"), DEFAULT_BOARD_ID)

    def test_unknown_model_uses_conservative_default(self):
        self.assertEqual(detect_board_id(""), DEFAULT_BOARD_ID)
        self.assertEqual(detect_board_id("some-other-sbc"), "raspberry_pi_4")

    def test_missing_device_tree_reads_empty(self):
        # nonexistent path -> '' -> default board (mirrors driver OSError handling)
        self.assertEqual(read_device_tree_model("/no/such/proc/model"), "")
        self.assertEqual(detect_board_id(read_device_tree_model("/no/such/proc/model")), DEFAULT_BOARD_ID)


class TestProfiles(unittest.TestCase):
    def test_every_profile_id_matches_its_key(self):
        for key, prof in PROFILES.items():
            self.assertEqual(key, prof.id)

    def test_default_board_is_a_real_profile(self):
        self.assertIn(DEFAULT_BOARD_ID, PROFILES)

    def test_led_transport_per_board(self):
        self.assertEqual(PROFILES["raspberry_pi_4"].led.transport, "pwm")
        self.assertEqual(PROFILES["raspberry_pi_4"].led.pwm_pin, 12)
        self.assertEqual(PROFILES["raspberry_pi_5"].led.transport, "spi")
        self.assertEqual((PROFILES["raspberry_pi_5"].led.spi_bus, PROFILES["raspberry_pi_5"].led.spi_device), (0, 0))
        self.assertEqual(PROFILES["orangepi_sun60"].led.transport, "spi")
        self.assertEqual(PROFILES["orangepi_sun60"].led.spi_bus, 3)

    def test_button_wiring_per_board(self):
        # Pi 4/5 share the wm8960 button on gpiochip0 line 17
        for b in ("raspberry_pi_4", "raspberry_pi_5"):
            self.assertEqual((PROFILES[b].button.chip, PROFILES[b].button.line), (0, 17))
        # OrangePi sun60: header pin 11 = PL9 -> gpiochip1 line 9
        self.assertEqual((PROFILES["orangepi_sun60"].button.chip, PROFILES["orangepi_sun60"].button.line), (1, 9))
        self.assertEqual(PROFILES["orangepi_sun60"].button.debounce_ns, 200_000_000)

    def test_touch_only_on_orangepi(self):
        self.assertIsNone(PROFILES["raspberry_pi_4"].touch)
        self.assertIsNone(PROFILES["raspberry_pi_5"].touch)
        self.assertEqual(PROFILES["orangepi_sun60"].touch.chip, 0)
        self.assertEqual(PROFILES["orangepi_sun60"].touch.lines, [96, 97, 98, 99])


class TestBoardProfileCaching(unittest.TestCase):
    def test_board_profile_returns_a_known_profile(self):
        # On a dev machine /proc/device-tree/model is absent -> default board.
        board_profile.cache_clear()
        self.assertIn(board_profile().id, PROFILES)


if __name__ == "__main__":
    unittest.main()
