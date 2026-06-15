"""Compatibility Test Suite — enforces contract/COMPATIBILITY.md against every device.

Static, no hardware: validates each devices/<id>/DEVICE.md against the MUST rules.
Reuses the HAL's DEVICE.md parser so the test and the runtime read the contract the same way.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "os"))

from hal.board.device import load_device  # noqa: E402  (path set above)

DEVICES_DIR = os.path.join(ROOT, "devices")

# The frozen capability vocabulary (contract/capabilities.md).
KNOWN_CAPABILITIES = {
    "audio", "vision", "sensing", "presence", "motion",
    "light", "display", "media", "connectivity", "companion", "system",
}
# Capabilities that can move, heat, or emit — they require a SAFETY.md.
SAFETY_CLASS = {"motion", "light"}
# Non-device folders under devices/.
NOT_DEVICES = {"_base", "examples"}


def real_devices():
    for name in sorted(os.listdir(DEVICES_DIR)):
        if name in NOT_DEVICES:
            continue
        if os.path.isfile(os.path.join(DEVICES_DIR, name, "DEVICE.md")):
            yield name


class TestCompatibility(unittest.TestCase):
    def test_at_least_one_device_exists(self):
        self.assertTrue(list(real_devices()), "no devices found under devices/")

    def test_every_device_is_compliant(self):
        for dev in real_devices():
            with self.subTest(device=dev):
                profile = load_device(dev, DEVICES_DIR)
                groups = set(profile.capabilities)
                with open(os.path.join(DEVICES_DIR, dev, "DEVICE.md")) as fh:
                    raw = fh.read()

                # MUST 1 — schema v1
                self.assertIn("schema: autonomous.device.v1", raw,
                              f"{dev}: DEVICE.md must declare schema autonomous.device.v1")
                # MUST 2 — system capability
                self.assertIn("system", groups, f"{dev}: must declare the 'system' capability")
                # MUST 3 — a primary sense or output
                self.assertTrue({"audio", "vision"} & groups,
                                f"{dev}: must declare a primary sense/output (audio or vision)")
                # MUST 4 — known capability vocabulary only
                unknown = groups - KNOWN_CAPABILITIES
                self.assertFalse(unknown, f"{dev}: declares unknown capabilities {unknown}")
                # MUST 6 — safety-class capability requires a SAFETY.md
                if SAFETY_CLASS & groups:
                    self.assertTrue(
                        os.path.isfile(os.path.join(DEVICES_DIR, dev, "SAFETY.md")),
                        f"{dev}: declares {SAFETY_CLASS & groups} but ships no SAFETY.md")


if __name__ == "__main__":
    unittest.main()
