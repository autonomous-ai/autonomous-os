"""Device button selection and malformed wiring checks, without GPIO access."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from hal.board.gpio_button import ButtonConfig, load_button_config, load_button_configs


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

    def test_lamp_has_independent_primary_and_reset_inputs(self):
        root = Path(__file__).resolve().parents[2] / "robots"
        buttons = load_button_configs(root / "lamp", "orangepi_sun60")
        self.assertEqual([(b.name, b.wiring.chip, b.wiring.line, b.behavior, b.hold_s)
                          for b in buttons],
                         [("primary", 0, 100, "standard", 5.0),
                          ("factory_reset", 0, 99, "factory_reset", 5.0)])
        self.assertEqual(len(load_button_configs(root / "intern-v2", "orangepi_sun60")), 1)
        with tempfile.TemporaryDirectory() as directory:
            fallback = load_button_configs(directory, "orangepi_sun60")
            self.assertEqual(len(fallback), 1)
            self.assertEqual(fallback[0].wiring, ButtonConfig(1, 9, 200_000_000))
            self.assertEqual(fallback[0].behavior, "standard")

    def test_multiple_inputs_reject_ambiguous_or_invalid_actions(self):
        primary = dict(name="primary", chip=0, line=100, debounce_ns=200)
        reset = dict(name="reset", chip=0, line=99, debounce_ns=200,
                     behavior="factory_reset", hold_s=5)
        cases = [[], [primary, dict(reset, line=100)],
                 [primary, dict(reset, name="primary")],
                 [dict(reset, hold_s=0)], [dict(reset, hold_s=-1)],
                 [dict(reset, hold_s=True)], [dict(reset, hold_s=float("inf"))],
                 [dict(reset, hold_s=float("nan"))], [dict(reset, behavior="reboot")],
                 [dict(primary, hold_s=5)], [dict(reset, name="reset\\ninvalid")],
                 [dict(reset, extra=True)]]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gpio_button.json"
            for buttons in cases:
                with self.subTest(buttons=buttons):
                    path.write_text(json.dumps({"boards": {"orangepi_sun60": {"buttons": buttons}}}))
                    with self.assertRaisesRegex(ValueError, "gpio_button.json"):
                        load_button_configs(directory, "orangepi_sun60")

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
