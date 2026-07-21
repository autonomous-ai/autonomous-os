"""Tests for the motion driver factory (hal/drivers/motors/factory.py).

Pure logic, no hardware. Also parses the REAL committed devices/reachy-mini
DEVICE.md to guard the declared driver name against drift, and AST-checks
ReachyMotionService against the MotionService protocol (the reachy_mini SDK
is not installed on dev machines, so the module can't be imported here).
"""
import ast
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

    def test_reachy_sdk_is_registered(self):
        self.assertIn("reachy_sdk", MOTION_DRIVERS)
        module_path, class_name = MOTION_DRIVERS["reachy_sdk"]
        self.assertEqual(class_name, "ReachyMotionService")

    def test_reachy_sdk_resolves_or_degrades(self):
        # On dev machines the reachy-mini package is absent → registered driver
        # import fails → None (plan_mounts handles required); on-device it
        # resolves to the class. Either way it must not raise.
        cls = resolve_motion_class("reachy_sdk", required=True)
        if cls is not None:
            self.assertEqual(cls.__name__, "ReachyMotionService")


# Method/property surface of the MotionService protocol (hal/drivers/motors/base.py).
_PROTOCOL_MEMBERS = {
    "start", "stop", "is_connected",
    "dispatch", "get_available_recordings", "add_recording", "ensure_running",
    "is_suppressed",
    "freeze", "unfreeze", "is_frozen",
    "move_to", "move_and_hold", "get_joint_names", "get_positions",
    "send_positions",
    "zero_pose", "release", "resume", "hold", "joint_status",
    "aim", "nudge",
}


class TestReachyServiceConformance(unittest.TestCase):
    """AST-level protocol conformance — no reachy_mini import needed."""

    def test_reachy_service_implements_protocol_surface(self):
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "drivers", "motors", "reachy_service.py",
        )
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())

        cls = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.ClassDef) and n.name == "ReachyMotionService"),
            None,
        )
        self.assertIsNotNone(cls, "ReachyMotionService class not found")

        defined = {
            n.name for n in cls.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        missing = _PROTOCOL_MEMBERS - defined
        self.assertFalse(
            missing, f"ReachyMotionService missing protocol members: {sorted(missing)}"
        )


if __name__ == "__main__":
    unittest.main()
