"""Tests for the per-device preset overlay (board/presets_overlay.py).

Pure logic, no hardware. The overlay mutates the module-level preset tables in
place, so each test deep-copies and restores them to stay isolated.
"""
import copy
import json
import os
import tempfile
import unittest

from hal import presets
from hal.board.presets_overlay import (
    DEFAULT_LED_COUNT,
    _merge_table,
    apply_device_presets,
)


class TestMergeTable(unittest.TestCase):
    def test_patches_only_named_fields(self):
        base = {"listening": {"color": [51, 121, 230], "effect": "pulse", "speed": 1.5}}
        _merge_table("emotion", base, {"listening": {"color": [255, 120, 0]}}, "demo")
        # Only color changed; effect + speed kept from the base entry.
        self.assertEqual(base["listening"], {"color": [255, 120, 0], "effect": "pulse", "speed": 1.5})

    def test_leaves_other_entries_untouched(self):
        base = {"listening": {"color": [1, 1, 1]}, "happy": {"color": [2, 2, 2]}}
        _merge_table("emotion", base, {"listening": {"color": [9, 9, 9]}}, "demo")
        self.assertEqual(base["happy"], {"color": [2, 2, 2]})

    def test_unknown_preset_key_fails_loud(self):
        base = {"listening": {"color": [1, 1, 1]}}
        with self.assertRaises(ValueError) as cm:
            _merge_table("emotion", base, {"listenign": {"color": [9, 9, 9]}}, "demo")
        self.assertIn("listenign", str(cm.exception))

    def test_non_dict_section_fails(self):
        with self.assertRaises(ValueError):
            _merge_table("emotion", {}, ["not", "a", "dict"], "demo")

    def test_non_dict_entry_fails(self):
        with self.assertRaises(ValueError):
            _merge_table("emotion", {"listening": {}}, {"listening": [1, 2, 3]}, "demo")


class TestApplyDevicePresets(unittest.TestCase):
    def setUp(self):
        # Snapshot the real module tables; restore after each test so mutation
        # never leaks across tests (other test modules import these too).
        self._emotion = copy.deepcopy(presets.EMOTION_PRESETS)
        self._scene = copy.deepcopy(presets.SCENE_PRESETS)
        self._aim = copy.deepcopy(presets.AIM_PRESETS)

    def tearDown(self):
        presets.EMOTION_PRESETS.clear()
        presets.EMOTION_PRESETS.update(self._emotion)
        presets.SCENE_PRESETS.clear()
        presets.SCENE_PRESETS.update(self._scene)
        presets.AIM_PRESETS.clear()
        presets.AIM_PRESETS.update(self._aim)

    def _write(self, tmp, device_type, payload):
        d = os.path.join(tmp, device_type)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "presets.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def test_no_file_keeps_base_and_default_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            count = apply_device_presets("ghost", tmp)
        self.assertEqual(count, DEFAULT_LED_COUNT)
        self.assertEqual(presets.EMOTION_PRESETS["listening"]["color"], self._emotion["listening"]["color"])

    def test_overrides_color_and_led_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "demo", {
                "led_count": 60,
                "emotion": {"listening": {"color": [255, 120, 0]}},
            })
            count = apply_device_presets("demo", tmp)
        self.assertEqual(count, 60)
        self.assertEqual(presets.EMOTION_PRESETS["listening"]["color"], [255, 120, 0])
        # Untouched field on the same entry survives.
        self.assertEqual(presets.EMOTION_PRESETS["listening"]["effect"],
                         self._emotion["listening"]["effect"])

    def test_overrides_scene_and_aim(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "demo", {
                "scene": {"relax": {"brightness": 0.3}},
                "aim": {"desk": {"base_pitch.pos": 8.0}},
            })
            apply_device_presets("demo", tmp)
        self.assertEqual(presets.SCENE_PRESETS["relax"]["brightness"], 0.3)
        self.assertEqual(presets.AIM_PRESETS["desk"]["base_pitch.pos"], 8.0)

    def test_unknown_emotion_fails_loud(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "demo", {"emotion": {"nope": {"color": [1, 1, 1]}}})
            with self.assertRaises(ValueError):
                apply_device_presets("demo", tmp)

    def test_bad_led_count_fails(self):
        for bad in (0, -5, True, "64", 1.5):
            with self.subTest(bad=bad):
                with tempfile.TemporaryDirectory() as tmp:
                    self._write(tmp, "demo", {"led_count": bad})
                    with self.assertRaises(ValueError):
                        apply_device_presets("demo", tmp)

    def test_malformed_json_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "demo")
            os.makedirs(d)
            with open(os.path.join(d, "presets.json"), "w", encoding="utf-8") as f:
                f.write("{not json")
            with self.assertRaises(json.JSONDecodeError):
                apply_device_presets("demo", tmp)

    def test_ignores_comment_and_unknown_top_level_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "demo", {
                "_comment": "this is a doc string, not a section",
                "future_field": 123,
                "emotion": {"listening": {"color": [1, 2, 3]}},
            })
            count = apply_device_presets("demo", tmp)
        self.assertEqual(count, DEFAULT_LED_COUNT)
        self.assertEqual(presets.EMOTION_PRESETS["listening"]["color"], [1, 2, 3])

    def test_shipped_example_file_is_valid(self):
        # The committed devices/_base/presets.example.json must always apply
        # cleanly — it is the copy-paste reference, so a typo there is a bug.
        here = os.path.dirname(os.path.abspath(__file__))
        example = os.path.normpath(
            os.path.join(here, "..", "..", "..", "devices", "_base", "presets.example.json")
        )
        with open(example, "r", encoding="utf-8") as f:
            payload = json.load(f)
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "demo", payload)
            count = apply_device_presets("demo", tmp)
        self.assertIsInstance(count, int)


if __name__ == "__main__":
    unittest.main()
