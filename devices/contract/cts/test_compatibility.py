"""Compatibility Test Suite — enforces devices/contract/COMPATIBILITY.md against every device.

Static, no hardware: validates each devices/<id>/ROBOT.md against the MUST rules.
Reuses the HAL's declaration parser so the test and the runtime read the contract the same way.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))  # devices/contract/cts → repo root
sys.path.insert(0, ROOT)  # the hal package lives at the repo root

from hal.board.device import load_device, profile_path  # noqa: E402  (path set above)
from hal.safety.policy import parse_safety  # noqa: E402  (path set above)

DEVICES_DIR = os.path.join(ROOT, "devices")

# The frozen capability vocabulary (devices/contract/capabilities.md).
KNOWN_CAPABILITIES = {
    "audio", "vision", "sensing", "presence", "motion",
    "light", "display", "expression", "lifelike", "media", "connectivity", "companion",
    "system",
}
# Capabilities that can move, heat, or emit — they require a SAFETY.md.
SAFETY_CLASS = {"motion", "light"}
# Non-device folders under devices/: anything underscore-prefixed (`_base`,
# `_template`) is a profile or scaffold, not a body.
NOT_DEVICES = {"examples"}


def real_devices():
    for name in sorted(os.listdir(DEVICES_DIR)):
        if name in NOT_DEVICES or name.startswith("_"):
            continue
        if os.path.isfile(profile_path(os.path.join(DEVICES_DIR, name))):
            yield name


class TestCompatibility(unittest.TestCase):
    def test_at_least_one_device_exists(self):
        self.assertTrue(list(real_devices()), "no devices found under devices/")

    def test_every_device_is_compliant(self):
        for dev in real_devices():
            with self.subTest(device=dev):
                profile = load_device(dev, DEVICES_DIR)
                groups = set(profile.capabilities)
                with open(profile_path(os.path.join(DEVICES_DIR, dev))) as fh:
                    raw = fh.read()

                # MUST 1 — schema v1
                self.assertIn("schema: autonomous.device.v1", raw,
                              f"{dev}: ROBOT.md must declare schema autonomous.device.v1")
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
                    safety_path = os.path.join(DEVICES_DIR, dev, "SAFETY.md")
                    self.assertTrue(os.path.isfile(safety_path),
                                    f"{dev}: declares {SAFETY_CLASS & groups} but ships no SAFETY.md")
                    with open(safety_path) as fh:
                        try:
                            parse_safety(fh.read())
                        except ValueError as exc:
                            self.fail(f"{dev}: SAFETY.md is not a valid safety policy: {exc}")


if __name__ == "__main__":
    unittest.main()
