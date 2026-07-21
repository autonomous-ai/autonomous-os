"""Tests for the motion driver factory (hal/drivers/motors/factory.py).

Pure logic, no hardware. Also parses the REAL committed devices/reachy-mini
DEVICE.md to guard the declared driver name against drift.
"""
import os
import unittest

from hal.board.device import extract_front_matter, parse_capabilities
from hal.drivers.motors.factory import MOTION_DRIVERS, resolve_motion_class

HERE = os.path.dirname(os.path.abspath(__file__))
# hal/test -> hal -> repo root
DEVICES_DIR = os.path.normpath(os.path.join(HERE, "..", "..", "devices"))


class TestResolveMotionClass(unittest.TestCase):
    def test_feetech_resolves_to_animation_service(self):
        cls = resolve_motion_class("feetech", required=True)
        self.assertIsNotNone(cls)
        self.assertEqual(cls.__name__, "AnimationService")

    def test_absent_driver_defaults_to_feetech(self):
        # Schema v1 compat: no driver declared → same class as explicit feetech.
        default_cls = resolve_motion_class(None, required=True)
        feetech_cls = resolve_motion_class("feetech", required=True)
        self.assertIs(default_cls, feetech_cls)

    def test_unknown_driver_required_fails_loud(self):
        with self.assertRaises(RuntimeError) as ctx:
            resolve_motion_class("no_such_driver", required=True)
        msg = str(ctx.exception)
        self.assertIn("no_such_driver", msg)   # names the offending driver
        self.assertIn("feetech", msg)          # lists the registered set

    def test_unknown_driver_optional_returns_none(self):
        self.assertIsNone(resolve_motion_class("no_such_driver", required=False))

    def test_registry_entries_are_module_class_pairs(self):
        for driver, entry in MOTION_DRIVERS.items():
            self.assertEqual(len(entry), 2, f"{driver}: expected (module, class)")


class TestReachyMiniDeclaration(unittest.TestCase):
    """Guard the committed reachy-mini DEVICE.md: motion declares reachy_sdk."""

    def _load_caps(self):
        path = os.path.join(DEVICES_DIR, "reachy-mini", "DEVICE.md")
        with open(path, encoding="utf-8") as f:
            return parse_capabilities(extract_front_matter(f.read()))

    def test_motion_driver_is_reachy_sdk(self):
        caps = self._load_caps()
        self.assertIn("motion", caps)
        self.assertEqual(caps["motion"].driver, "reachy_sdk")
        self.assertTrue(caps["motion"].required)

    def test_unregistered_reachy_sdk_fails_loud_today(self):
        # Until ReachyMotionService lands, booting reachy-mini must fail loud,
        # not silently fall back to another backend.
        caps = self._load_caps()
        if caps["motion"].driver in MOTION_DRIVERS:
            self.skipTest("reachy_sdk driver is now registered")
        with self.assertRaises(RuntimeError):
            resolve_motion_class(caps["motion"].driver, caps["motion"].required)


if __name__ == "__main__":
    unittest.main()
