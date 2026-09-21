"""Real staged rendering, including byte-identical legacy upgrades."""

import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[3]
SOURCE = REPO / "robots/lamp"
spec = importlib.util.spec_from_file_location("overrides", REPO / "scripts/provision/apply-overrides.py")
overrides = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overrides)


class DeviceOverrideTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.root / "staged-device"
        for name in ("ROBOT.md", "SAFETY.md", "rootfs/opt/hal/.env", "rootfs/etc/asound.conf"):
            dest = self.profile / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE / name, dest)
        shutil.copytree(SOURCE / "overrides", self.profile / "overrides")

    def select(self, value):
        path = self.root / overrides.PROFILE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def snapshot(self):
        return {str(p.relative_to(self.profile)): p.read_bytes()
                for p in self.profile.rglob("*") if p.is_file()}

    def test_legacy_noop_for_missing_empty_or_standard_identity(self):
        shutil.rmtree(self.profile / "overrides")
        before = self.snapshot()
        self.assertIsNone(overrides.apply_overrides(self.profile, self.root))
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.root / overrides.PROFILE_PATH).exists())
        for value in ("", "\n", "standard\n", " standard \n"):
            self.select(value)
            self.assertIsNone(overrides.apply_overrides(self.profile, self.root))
            self.assertEqual(before, self.snapshot())

    def test_selected_audio_preserves_other_settings_and_identity(self):
        identity = self.select("pro\n")
        self.assertEqual(overrides.apply_overrides(self.profile, self.root), "pro")
        env = (self.profile / "rootfs/opt/hal/.env").read_text()
        for value in ("HAL_AEC_ENABLED=false", "HAL_LIVE_MODE=true",
                      "HAL_LIVE_UPLINK_DURING_PLAYBACK=always",
                      "HAL_VOLUME_STATE_PATH=/root/config/.volume-pro", "HAL_TTS_SPEED=1.1"):
            self.assertIn(value + "\n", env)
        self.assertIn("startup_volume: 35", (self.profile / "ROBOT.md").read_text())
        self.assertIn("max_volume: 35", (self.profile / "SAFETY.md").read_text())
        self.assertIn("max_speed: 120", (self.profile / "SAFETY.md").read_text())
        self.assertEqual((self.profile / "rootfs/etc/asound.conf").read_bytes(), (SOURCE / "overrides/pro/rootfs/etc/asound.conf").read_bytes())
        self.assertEqual(identity.read_text(), "pro\n")
        self.assertFalse((self.root / "opt/hal/.env").exists())

    def test_arbitrary_profile_uses_package_data_without_product_logic(self):
        (self.profile / "overrides/pro").rename(self.profile / "overrides/studio")
        (self.profile / "overrides/studio/profile.json").write_text(json.dumps({"startup_volume": 42, "max_volume": 55}))
        self.select("studio")
        self.assertEqual(overrides.apply_overrides(self.profile, self.root), "studio")
        self.assertIn("startup_volume: 42", (self.profile / "ROBOT.md").read_text())
        self.assertIn("max_volume: 55", (self.profile / "SAFETY.md").read_text())
        self.assertIn(".volume-studio", (self.profile / "rootfs/opt/hal/.env").read_text())

    def test_invalid_or_unknown_profile_fails_without_changes(self):
        for value in ("../pro", "/pro", "PRO", "unknown"):
            self.select(value)
            before = self.snapshot()
            with self.assertRaises((OSError, ValueError)):
                overrides.apply_overrides(self.profile, self.root)
            self.assertEqual(before, self.snapshot())

    def test_profile_cannot_overwrite_identity(self):
        self.select("pro")
        target = self.profile / "overrides/pro/rootfs" / overrides.PROFILE_PATH
        target.parent.mkdir(parents=True)
        target.write_text("standard")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "identity"):
            overrides.apply_overrides(self.profile, self.root)
        self.assertEqual(before, self.snapshot())

    def test_invalid_volume_policy_fails_without_changes(self):
        self.select("pro")
        (self.profile / "overrides/pro/profile.json").write_text('{"startup_volume":90,"max_volume":40}')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "exceeds"):
            overrides.apply_overrides(self.profile, self.root)
        self.assertEqual(before, self.snapshot())

    def test_repeat_render_and_new_ota_do_not_lose_selection(self):
        identity = self.select("pro")
        overrides.apply_overrides(self.profile, self.root)
        first = self.snapshot()
        overrides.apply_overrides(self.profile, self.root)
        self.assertEqual(first, self.snapshot())
        for name in ("ROBOT.md", "SAFETY.md", "rootfs/opt/hal/.env", "rootfs/etc/asound.conf"):
            shutil.copy2(SOURCE / name, self.profile / name)
        overrides.apply_overrides(self.profile, self.root)
        self.assertEqual(first, self.snapshot())
        self.assertEqual(identity.read_text(), "pro")

    def test_volume_state_is_selected_without_env_overlay(self):
        self.select("pro")
        (self.profile / "overrides/pro/rootfs/opt/hal/.env").unlink()
        overrides.apply_overrides(self.profile, self.root)
        self.assertIn("HAL_VOLUME_STATE_PATH=/root/config/.volume-pro", (self.profile / "rootfs/opt/hal/.env").read_text())


if __name__ == "__main__":
    unittest.main()
