"""Tests for the device-profile layer: DEVICE.md parsing + mount planning.

Pure logic, no hardware. Also parses the REAL committed devices/lamp and
devices/intern DEVICE.md files to guard the contract against drift.
"""
import os
import unittest

from lelamp.platform.device import (
    MountPlan,
    extract_front_matter,
    load_device,
    parse_capabilities,
    parse_device,
    plan_mounts,
)

HERE = os.path.dirname(os.path.abspath(__file__))
# test -> lelamp -> hal -> os -> repo root
DEVICES_DIR = os.path.normpath(os.path.join(HERE, "..", "..", "..", "..", "devices"))

SAMPLE = """---
schema: autonomous.device.v1
id: sample
capabilities:
  audio:  { routes: [audio, speaker, voice], required: true }
  motion: { routes: [servo], driver: feetech, required: false, safety: SAFETY.md#motion }
  system: { routes: [system], required: true }
soul_ref: autonomous://souls/sample
---

# body text ignored
"""


class TestParsing(unittest.TestCase):
    def test_extract_front_matter(self):
        fm = extract_front_matter(SAMPLE)
        self.assertIn("capabilities:", fm)
        self.assertNotIn("body text ignored", fm)

    def test_parse_capabilities_flow_style(self):
        caps = parse_capabilities(extract_front_matter(SAMPLE))
        self.assertEqual(set(caps), {"audio", "motion", "system"})
        self.assertEqual(caps["audio"].routes, ["audio", "speaker", "voice"])
        self.assertTrue(caps["audio"].required)
        self.assertEqual(caps["motion"].routes, ["servo"])
        self.assertFalse(caps["motion"].required)

    def test_declared_routes_required_rollup(self):
        dev = parse_device("sample", SAMPLE)
        routes = dev.declared_routes()
        self.assertTrue(routes["audio"])      # required cap
        self.assertFalse(routes["servo"])     # optional cap
        self.assertTrue(routes["system"])


class TestRealDeviceFiles(unittest.TestCase):
    def test_lamp_is_maximal(self):
        lamp = load_device("lamp", DEVICES_DIR)
        groups = set(lamp.capabilities)
        # Lamp is the maximal device: it has motion AND display.
        self.assertIn("motion", groups)
        self.assertIn("display", groups)
        self.assertTrue(lamp.capabilities["audio"].required)

    def test_intern_is_lamp_minus_motion_and_display(self):
        lamp = set(load_device("lamp", DEVICES_DIR).capabilities)
        intern = set(load_device("intern", DEVICES_DIR).capabilities)
        # Intern strips Lamp's expressive/actuation capabilities; motion + display
        # are the headline removals, and Intern adds nothing Lamp lacks.
        self.assertEqual(lamp - intern, {"presence", "motion", "light", "display", "media", "connectivity"})
        self.assertEqual(intern - lamp, set())
        self.assertNotIn("motion", intern)
        self.assertNotIn("display", intern)
        self.assertIn("audio", intern)
        self.assertTrue(load_device("intern", DEVICES_DIR).capabilities["audio"].required)


class TestMountPlanning(unittest.TestCase):
    def test_declared_present_is_mounted(self):
        plan = plan_mounts({"audio": True, "servo": False}, {"audio": True, "servo": True})
        self.assertEqual(set(plan.mounted), {"audio", "servo"})
        self.assertTrue(plan.ok)

    def test_declared_required_but_missing_fails_loud(self):
        plan = plan_mounts({"audio": True}, {"audio": False})
        self.assertEqual(plan.failed_required, ["audio"])
        self.assertFalse(plan.ok)

    def test_declared_optional_but_missing_skips_gracefully(self):
        plan = plan_mounts({"servo": False}, {"servo": False})
        self.assertIn("servo", plan.skipped)
        self.assertEqual(plan.failed_required, [])
        self.assertTrue(plan.ok)

    def test_undeclared_is_skipped_not_mounted(self):
        # Intern: servo driver present on the image but NOT declared -> never mounts.
        plan = plan_mounts({"audio": True}, {"audio": True, "servo": True})
        self.assertIn("servo", plan.skipped)
        self.assertNotIn("servo", plan.mounted)


if __name__ == "__main__":
    unittest.main()
