"""Tests for the safety policy layer — pure parser + gate, no hardware.

Covers the slice-1 (brightness ceiling) checklist in docs/safety.md:
unit clamp behavior, schema fail-loud, and the load_safety fail-safe rules.
"""
import os
import tempfile
import unittest
from datetime import time as dtime

from hal.safety.policy import (
    MotionBounds,
    QuietHours,
    SafetyPolicy,
    ThermalBounds,
    active_max_brightness,
    audio_quiet_now,
    clamp_brightness,
    clamp_color,
    in_window,
    load_safety,
    min_move_duration,
    parse_safety,
    read_soc_temp_c,
    thermal_over,
    validate_schema,
)

_FM_THERMAL = "---\nschema: autonomous.safety.v1\nthermal:\n  max_temp_c: 95\n---\n"

_FM_MOTION = (
    "---\n"
    "schema: autonomous.safety.v1\n"
    "motion:\n"
    "  max_speed: 120\n"
    "  stop_always: true\n"
    "  # max_accel: <int>\n"   # comment must NOT be parsed as a bound
    "---\n"
)

_FM = "---\nschema: autonomous.safety.v1\nlight:\n  max_brightness: 180\n---\n# prose\n"

_FM_QUIET = (
    "---\n"
    "schema: autonomous.safety.v1\n"
    "light:\n"
    "  max_brightness: 180\n"
    '  quiet_hours: { start: "22:00", end: "07:00", max_brightness: 40 }\n'
    "audio:\n"
    '  quiet_hours: { start: "22:00", end: "07:00" }\n'
    "---\n"
)


class TestParse(unittest.TestCase):
    def test_parse_full(self):
        p = parse_safety(_FM)
        self.assertEqual(p.schema, "autonomous.safety.v1")
        self.assertEqual(p.max_brightness, 180)

    def test_parse_no_light_bound(self):
        p = parse_safety("---\nschema: autonomous.safety.v1\n---\n")
        self.assertEqual(p.max_brightness, None)

    def test_parse_flow_style(self):
        p = parse_safety("---\nschema: autonomous.safety.v1\nlight: { max_brightness: 90 }\n---\n")
        self.assertEqual(p.max_brightness, 90)

    def test_out_of_range_raises(self):
        with self.assertRaises(ValueError):
            parse_safety("---\nschema: autonomous.safety.v1\nlight:\n  max_brightness: 300\n---\n")


class TestSchemaValidation(unittest.TestCase):
    def test_missing_schema_raises(self):
        with self.assertRaises(ValueError):
            validate_schema("light:\n  max_brightness: 180\n")

    def test_malformed_schema_raises(self):
        with self.assertRaises(ValueError):
            validate_schema("schema: not.a.valid.tag\n")

    def test_unknown_major_raises(self):
        with self.assertRaises(ValueError):
            validate_schema("schema: autonomous.safety.v2\n")

    def test_valid_schema_passes(self):
        self.assertEqual(validate_schema("schema: autonomous.safety.v1\n"), "autonomous.safety.v1")


class TestClampBrightness(unittest.TestCase):
    def setUp(self):
        self.p = SafetyPolicy(schema="autonomous.safety.v1", max_brightness=180)

    def test_above_ceiling_clamps(self):
        self.assertEqual(clamp_brightness(self.p, 255), 180)

    def test_below_ceiling_passes(self):
        self.assertEqual(clamp_brightness(self.p, 120), 120)

    def test_no_policy_passes_through(self):
        self.assertEqual(clamp_brightness(None, 255), 255)

    def test_no_ceiling_passes_through(self):
        p = SafetyPolicy(schema="autonomous.safety.v1", max_brightness=None)
        self.assertEqual(clamp_brightness(p, 255), 255)


class TestClampColor(unittest.TestCase):
    def setUp(self):
        self.p = SafetyPolicy(schema="autonomous.safety.v1", max_brightness=180)

    def test_full_white_clamps_to_ceiling(self):
        self.assertEqual(clamp_color(self.p, (255, 255, 255)), (180, 180, 180))

    def test_hue_preserved_when_scaling(self):
        # pure red at full -> scaled to ceiling, still pure red
        self.assertEqual(clamp_color(self.p, (255, 0, 0)), (180, 0, 0))

    def test_below_ceiling_unchanged(self):
        self.assertEqual(clamp_color(self.p, (100, 50, 0)), (100, 50, 0))

    def test_no_policy_passes_through(self):
        self.assertEqual(clamp_color(None, (255, 255, 255)), (255, 255, 255))


class TestLoadSafety(unittest.TestCase):
    def _write(self, name, text):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, name), "w") as f:
            f.write(text)
        return d

    def test_no_safety_ref_returns_none(self):
        self.assertIsNone(load_safety("/tmp", ""))

    def test_valid_file_loads(self):
        d = self._write("SAFETY.md", _FM)
        p = load_safety(d, "SAFETY.md")
        self.assertEqual(p.max_brightness, 180)

    def test_prose_only_returns_none(self):
        # SAFETY.md with no front matter -> pass-through (legacy prose), not a crash
        d = self._write("SAFETY.md", "# SAFETY.md\n\nNo front matter here.\n")
        self.assertIsNone(load_safety(d, "SAFETY.md"))

    def test_missing_file_returns_none(self):
        # declared safety_ref but no file -> pass-through + warn, not a crash
        self.assertIsNone(load_safety(tempfile.mkdtemp(), "SAFETY.md"))

    def test_bad_schema_file_raises(self):
        # present front matter with an unknown major -> fail loud (abort boot)
        d = self._write("SAFETY.md", "---\nschema: autonomous.safety.v9\n---\n")
        with self.assertRaises(ValueError):
            load_safety(d, "SAFETY.md")


class TestQuietHoursParse(unittest.TestCase):
    def test_parses_both_windows_and_base(self):
        p = parse_safety(_FM_QUIET)
        # base ceiling is the light max_brightness, NOT the quiet one
        self.assertEqual(p.max_brightness, 180)
        self.assertEqual(p.light_quiet, QuietHours(dtime(22, 0), dtime(7, 0), 40))
        self.assertEqual(p.audio_quiet, QuietHours(dtime(22, 0), dtime(7, 0), None))

    def test_no_quiet_hours(self):
        p = parse_safety(_FM)
        self.assertIsNone(p.light_quiet)
        self.assertIsNone(p.audio_quiet)


class TestInWindow(unittest.TestCase):
    def setUp(self):
        self.wrap = QuietHours(dtime(22, 0), dtime(7, 0))      # crosses midnight
        self.same = QuietHours(dtime(9, 0), dtime(17, 0))      # same day

    def test_wrap_evening_inside(self):
        self.assertTrue(in_window(self.wrap, dtime(23, 0)))

    def test_wrap_early_morning_inside(self):
        self.assertTrue(in_window(self.wrap, dtime(6, 0)))

    def test_wrap_daytime_outside(self):
        self.assertFalse(in_window(self.wrap, dtime(12, 0)))

    def test_wrap_boundary_end_exclusive(self):
        self.assertFalse(in_window(self.wrap, dtime(7, 0)))

    def test_same_day_inside_outside(self):
        self.assertTrue(in_window(self.same, dtime(10, 0)))
        self.assertFalse(in_window(self.same, dtime(8, 0)))


class TestQuietHoursGate(unittest.TestCase):
    def setUp(self):
        self.p = parse_safety(_FM_QUIET)

    def test_ceiling_reduced_inside_window(self):
        self.assertEqual(active_max_brightness(self.p, dtime(23, 0)), 40)

    def test_ceiling_base_outside_window(self):
        self.assertEqual(active_max_brightness(self.p, dtime(12, 0)), 180)

    def test_clamp_color_night_vs_day(self):
        # full white: clamps to 40 at night, 180 by day (real wall-clock injected)
        self.assertEqual(clamp_color(self.p, (255, 255, 255), dtime(23, 0)), (40, 40, 40))
        self.assertEqual(clamp_color(self.p, (255, 255, 255), dtime(12, 0)), (180, 180, 180))

    def test_clamp_brightness_night(self):
        self.assertEqual(clamp_brightness(self.p, 200, dtime(2, 0)), 40)
        self.assertEqual(clamp_brightness(self.p, 30, dtime(2, 0)), 30)

    def test_audio_quiet_now(self):
        self.assertTrue(audio_quiet_now(self.p, dtime(23, 30)))
        self.assertFalse(audio_quiet_now(self.p, dtime(15, 0)))

    def test_audio_quiet_none_when_no_policy(self):
        self.assertFalse(audio_quiet_now(None, dtime(23, 30)))
        self.assertFalse(audio_quiet_now(parse_safety(_FM), dtime(23, 30)))


class TestMotionParse(unittest.TestCase):
    def test_parses_motion(self):
        p = parse_safety(_FM_MOTION)
        self.assertEqual(p.motion, MotionBounds(max_speed=120, stop_always=True))

    def test_no_motion_section(self):
        self.assertIsNone(parse_safety(_FM).motion)

    def test_commented_stop_always_not_a_bound(self):
        # a motion section with ONLY commented placeholders → no real bounds → None
        fm = "---\nschema: autonomous.safety.v1\nmotion:\n  # stop_always: true\n  # max_speed: <int>\n---\n"
        self.assertIsNone(parse_safety(fm).motion)

    def test_bad_max_speed_raises(self):
        with self.assertRaises(ValueError):
            parse_safety("---\nschema: autonomous.safety.v1\nmotion:\n  max_speed: 0\n---\n")


class TestMinMoveDuration(unittest.TestCase):
    def setUp(self):
        self.p = parse_safety(_FM_MOTION)  # max_speed 120 deg/s

    def test_stretches_when_too_fast(self):
        # 120 deg move requested in 0.1s -> needs 1.0s at 120 deg/s
        d = min_move_duration(self.p, {"pan.pos": 120.0}, {"pan.pos": 0.0}, 0.1)
        self.assertAlmostEqual(d, 1.0, places=3)

    def test_passes_when_slow_enough(self):
        d = min_move_duration(self.p, {"pan.pos": 12.0}, {"pan.pos": 0.0}, 5.0)
        self.assertEqual(d, 5.0)

    def test_instant_request_bounded(self):
        # duration 0 with a real delta -> stretched to the speed-safe minimum
        d = min_move_duration(self.p, {"pan.pos": 60.0}, {"pan.pos": 0.0}, 0.0)
        self.assertAlmostEqual(d, 0.5, places=3)

    def test_no_speed_bound_passthrough(self):
        self.assertEqual(min_move_duration(parse_safety(_FM), {"a.pos": 99}, {"a.pos": 0}, 0.2), 0.2)

    def test_no_policy_passthrough(self):
        # no safety config at all → motion runs unclamped (presence-driven, the
        # same pass-through rule as light/audio; no fail-closed, no kill switch)
        self.assertEqual(min_move_duration(None, {"a.pos": 999}, {"a.pos": 0}, 0.01), 0.01)

    def test_unknown_current_joint_ignored(self):
        # no known start for the joint -> can't bound its speed -> requested kept
        self.assertEqual(min_move_duration(self.p, {"pan.pos": 200.0}, {}, 0.1), 0.1)


class TestParseThermal(unittest.TestCase):
    def test_parse_default_resume(self):
        t = parse_safety(_FM_THERMAL).thermal
        self.assertEqual(t, ThermalBounds(max_temp_c=95, resume_temp_c=85))  # resume = max - 10

    def test_explicit_resume(self):
        fm = "---\nschema: autonomous.safety.v1\nthermal:\n  max_temp_c: 95\n  resume_temp_c: 80\n---\n"
        self.assertEqual(parse_safety(fm).thermal, ThermalBounds(95, 80))

    def test_no_max_temp_is_none(self):
        fm = "---\nschema: autonomous.safety.v1\nthermal:\n  # max_temp_c: <int>\n---\n"
        self.assertIsNone(parse_safety(fm).thermal)

    def test_bad_max_temp_raises(self):
        with self.assertRaises(ValueError):
            parse_safety("---\nschema: autonomous.safety.v1\nthermal:\n  max_temp_c: 0\n---\n")

    def test_resume_not_below_max_raises(self):
        with self.assertRaises(ValueError):
            parse_safety("---\nschema: autonomous.safety.v1\nthermal:\n  max_temp_c: 90\n  resume_temp_c: 90\n---\n")


class TestThermalOver(unittest.TestCase):
    def setUp(self):
        self.p = parse_safety(_FM_THERMAL)  # max 95, resume 85

    def test_trips_at_or_above_max(self):
        self.assertTrue(thermal_over(self.p, 95.0, False))
        self.assertTrue(thermal_over(self.p, 96.5, False))

    def test_below_max_not_over(self):
        self.assertFalse(thermal_over(self.p, 94.0, False))

    def test_hysteresis_stays_over_above_resume(self):
        # already over, cooled to 90 (> resume 85) -> still over
        self.assertTrue(thermal_over(self.p, 90.0, True))

    def test_clears_at_or_below_resume(self):
        self.assertFalse(thermal_over(self.p, 85.0, True))
        self.assertFalse(thermal_over(self.p, 84.0, True))

    def test_no_policy_or_no_thermal_or_no_temp(self):
        self.assertFalse(thermal_over(None, 200.0, False))
        self.assertFalse(thermal_over(parse_safety(_FM), 200.0, False))  # no thermal section
        self.assertFalse(thermal_over(self.p, None, True))                # unreadable temp


class TestReadSocTemp(unittest.TestCase):
    def test_reads_millidegrees_as_celsius(self):
        with tempfile.NamedTemporaryFile("w", suffix=".temp", delete=False) as f:
            f.write("85123\n")
            path = f.name
        try:
            self.assertAlmostEqual(read_soc_temp_c(path), 85.123, places=3)
        finally:
            os.unlink(path)

    def test_unreadable_returns_none(self):
        self.assertIsNone(read_soc_temp_c("/nonexistent/thermal/zone"))


if __name__ == "__main__":
    unittest.main()
