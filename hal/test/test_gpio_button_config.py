"""Device button selection and malformed wiring checks, without GPIO access."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from hal.board.gpio_button import ButtonConfig, load_button_config


class TestButtonConfig(unittest.TestCase):
    def test_driver_claims_device_override(self):
        from hal.drivers.gpio_button import GPIOButtonHandler

        gpio = mock.Mock()
        config = ButtonConfig(2, 31, 10_000_000)
        with mock.patch.dict("sys.modules", {"lgpio": gpio}):
            handler = GPIOButtonHandler(config)
            handler.start()
        gpio.gpiochip_open.assert_called_once_with(2)
        gpio.gpio_claim_alert.assert_called_once_with(
            gpio.gpiochip_open.return_value, 31, gpio.BOTH_EDGES, gpio.SET_PULL_UP,
        )
        self.assertEqual(handler._debounce_ns, 10_000_000)

    def test_shipped_devices_select_their_wiring(self):
        root = Path(__file__).resolve().parents[2] / "robots"
        for device in ("lamp", "intern-v2"):
            for board, chip, line in (
                ("raspberry_pi_4", 0, 17),
                ("raspberry_pi_5", 0, 17),
                ("orangepi_sun60", 0 if device == "lamp" else 1,
                 100 if device == "lamp" else 9),
            ):
                with self.subTest(device=device, board=board):
                    self.assertEqual(
                        load_button_config(root / device, board),
                        ButtonConfig(chip, line, 200_000_000),
                    )
            self.assertEqual(load_button_config(root / device, "sim"),
                             ButtonConfig(0, 17, 200_000_000))

    def test_device_specific_pin_and_missing_declarations(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_button_config(directory, "orangepi_sun60"),
                             ButtonConfig(1, 9, 200_000_000))
            path = Path(directory) / "gpio_button.json"
            path.write_text(json.dumps({"boards": {"orangepi_sun60": {
                "chip": 2, "line": 31, "debounce_ns": 10_000_000,
            }}}))
            self.assertEqual(load_button_config(directory, "orangepi_sun60"),
                             ButtonConfig(2, 31, 10_000_000))
            self.assertEqual(load_button_config(directory, "raspberry_pi_4"),
                             ButtonConfig(0, 17, 200_000_000))

    def test_invalid_config_rejected_with_path(self):
        cases = ["{", "null", '{"boards": []}']
        for values in (
            {"chip": 0, "line": -1, "debounce_ns": 200},
            {"chip": False, "line": 17, "debounce_ns": 200},
            {"chip": 0, "line": "17", "debounce_ns": 200},
            {"chip": 0, "line": 17},
        ):
            cases.append(json.dumps({"boards": {"orangepi_sun60": values}}))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gpio_button.json"
            for text in cases:
                with self.subTest(text=text):
                    path.write_text(text)
                    with self.assertRaisesRegex(ValueError, "gpio_button.json"):
                        load_button_config(directory, "orangepi_sun60")


if __name__ == "__main__":
    unittest.main()
