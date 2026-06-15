"""Tests for the device-profile layer: DEVICE.md parsing + mount planning.

Pure logic, no hardware. Also parses the REAL committed devices/lamp and
devices/intern-v2 DEVICE.md files to guard the contract against drift.
"""
import os
import unittest

from hal.board.device import (
    Capability,
    MountPlan,
    extract_front_matter,
    load_device,
    parse_capabilities,
    parse_device,
    plan_mounts,
    validate_safety_refs,
    validate_schema,
)

HERE = os.path.dirname(os.path.abspath(__file__))
# test -> hal -> hal -> os -> repo root
DEVICES_DIR = os.path.normpath(os.path.join(HERE, "..", "..", "..", "devices"))

SAMPLE = """---
schema: autonomous.device.v1
id: sample
name: Sample Device
type: test_device
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

    def test_parse_capability_driver(self):
        caps = parse_capabilities(extract_front_matter(SAMPLE))
        self.assertEqual(caps["motion"].driver, "feetech")  # informational family
        self.assertIsNone(caps["audio"].driver)             # none declared

    def test_lamp_real_drivers(self):
        caps = load_device("lamp", DEVICES_DIR).capabilities
        self.assertEqual(caps["motion"].driver, "feetech")
        self.assertEqual(caps["light"].driver, "ws2812")
        self.assertEqual(caps["display"].driver, "gc9a01")

    def test_safety_ref_parsed(self):
        # SAMPLE declares no top-level safety_ref; lamp declares SAFETY.md.
        self.assertEqual(parse_device("sample", SAMPLE).safety_ref, "")
        self.assertEqual(load_device("lamp", DEVICES_DIR).safety_ref, "SAFETY.md")

    def test_memory_backend_parsed(self):
        # SAMPLE declares no memory block; lamp declares { backend: local }.
        self.assertEqual(parse_device("sample", SAMPLE).memory_backend, "")
        self.assertEqual(load_device("lamp", DEVICES_DIR).memory_backend, "local")

    def test_declared_routes_required_rollup(self):
        dev = parse_device("sample", SAMPLE)
        routes = dev.declared_routes()
        self.assertTrue(routes["audio"])      # required cap
        self.assertFalse(routes["servo"])     # optional cap
        self.assertTrue(routes["system"])


class TestSchemaValidation(unittest.TestCase):
    def test_parse_device_sets_schema(self):
        self.assertEqual(parse_device("sample", SAMPLE).schema, "autonomous.device.v1")

    def test_valid_schema_returns_tag(self):
        self.assertEqual(
            validate_schema("schema: autonomous.device.v1\n"), "autonomous.device.v1"
        )

    def test_missing_schema_fails_loud(self):
        with self.assertRaises(ValueError):
            validate_schema("id: x\ncapabilities:\n")

    def test_malformed_schema_fails_loud(self):
        with self.assertRaises(ValueError):
            validate_schema("schema: autonomous.device.1\n")  # no 'v'
        with self.assertRaises(ValueError):
            validate_schema("schema: some.other.v1\n")        # wrong namespace

    def test_unknown_major_fails_loud(self):
        with self.assertRaises(ValueError):
            validate_schema("schema: autonomous.device.v2\n")

    def test_real_devices_declare_v1(self):
        self.assertEqual(load_device("lamp", DEVICES_DIR).schema, "autonomous.device.v1")
        self.assertEqual(load_device("intern-v2", DEVICES_DIR).schema, "autonomous.device.v1")


class TestIdentityFields(unittest.TestCase):
    def test_parse_id_name_type(self):
        dev = parse_device("sample", SAMPLE)
        self.assertEqual(dev.id, "sample")
        self.assertEqual(dev.name, "Sample Device")
        self.assertEqual(dev.type, "test_device")

    def test_id_must_match_folder(self):
        # SAMPLE declares id: sample; loading it as a different device_type aborts.
        with self.assertRaises(ValueError):
            parse_device("other", SAMPLE)

    def test_real_devices_id_equals_folder(self):
        for t in ("lamp", "intern-v2", "unitree-go2w"):
            self.assertEqual(load_device(t, DEVICES_DIR).id, t)


class TestBoardsField(unittest.TestCase):
    def test_parse_boards_flow_list(self):
        dev = parse_device("sample", SAMPLE)
        self.assertEqual(dev.boards, [])  # SAMPLE declares none

    def test_lamp_declares_its_boards(self):
        lamp = load_device("lamp", DEVICES_DIR)
        self.assertIn("orangepi_sun60", lamp.boards)
        self.assertIn("raspberry_pi_5", lamp.boards)


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
        intern = set(load_device("intern-v2", DEVICES_DIR).capabilities)
        # Intern strips Lamp's expressive/actuation capabilities; motion + display
        # are the headline removals, and Intern adds nothing Lamp lacks.
        # NOTE: intern-v2's DEVICE.md frontmatter declares `light` (the machine
        # truth this test tracks). Its prose contradicts this — flagged to the HW
        # team separately; the device declaration is out of scope here.
        self.assertEqual(lamp - intern, {"vision", "motion", "presence", "display", "media", "connectivity"})
        self.assertEqual(intern - lamp, set())
        self.assertNotIn("motion", intern)
        self.assertNotIn("display", intern)
        self.assertIn("audio", intern)
        self.assertTrue(load_device("intern-v2", DEVICES_DIR).capabilities["audio"].required)


class TestSafetyRefs(unittest.TestCase):
    def test_parse_capabilities_sets_safety(self):
        caps = parse_capabilities(extract_front_matter(SAMPLE))
        self.assertEqual(caps["motion"].safety, "SAFETY.md#motion")

    def test_capability_without_safety_defaults_none(self):
        cap = Capability(group="audio", routes=["audio"], required=True)
        self.assertIsNone(cap.safety)

    def test_validate_clean_when_anchor_exists(self):
        dev = parse_device("sample", SAMPLE)
        problems = validate_safety_refs(dev, "# Safety\n\n## motion\n\nrules here\n")
        self.assertEqual(problems, [])

    def test_validate_warns_when_anchor_missing(self):
        dev = parse_device("sample", SAMPLE)
        problems = validate_safety_refs(dev, "# Safety\n\n## light\n\nrules here\n")
        self.assertTrue(problems)
        self.assertIn("motion", problems[0])

    def test_validate_warns_when_safety_md_empty(self):
        dev = parse_device("sample", SAMPLE)
        self.assertTrue(validate_safety_refs(dev, ""))

    def test_lamp_real_refs_validate_clean(self):
        lamp = load_device("lamp", DEVICES_DIR)
        safety_path = os.path.join(DEVICES_DIR, "lamp", "SAFETY.md")
        with open(safety_path, "r") as f:
            self.assertEqual(validate_safety_refs(lamp, f.read()), [])


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


class TestInternBootProof(unittest.TestCase):
    """Batch C boot-proof (no hardware): the same router set, gated by each
    device's DEVICE.md, yields different mounts — Intern is Lamp-minus, not a fork."""

    ALL_ROUTERS = {
        "servo", "led", "camera", "audio", "emotion", "scene",
        "sensing", "display", "voice", "music", "system", "bluetooth",
    }

    def _mounted(self, device_type):
        declared = set(load_device(device_type, DEVICES_DIR).declared_routes())
        return self.ALL_ROUTERS & declared

    def test_lamp_mounts_servo_and_display(self):
        m = self._mounted("lamp")
        self.assertIn("servo", m)
        self.assertIn("display", m)

    def test_intern_mounts_neither_servo_nor_display(self):
        m = self._mounted("intern-v2")
        self.assertNotIn("servo", m)
        self.assertNotIn("display", m)

    def test_both_mount_the_shared_audio_stack(self):
        lamp, intern = self._mounted("lamp"), self._mounted("intern-v2")
        for route in ("audio", "voice", "system"):
            self.assertIn(route, lamp)
            self.assertIn(route, intern)

    def test_intern_is_a_strict_subset_of_lamp(self):
        self.assertTrue(self._mounted("intern-v2") < self._mounted("lamp"))


if __name__ == "__main__":
    unittest.main()
