"""Run the real release script against an isolated filesystem and fake GCS.

Usage: python3 scripts/release/tests/test_upload_autonomous_buddy.py
Requires the release script's local tools (bash, jq, shasum); no GCS access.
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


RELEASE_DIR = Path(__file__).resolve().parents[1]


class UploadBuddyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="buddy-release-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        release = self.root / "scripts/release"
        release.mkdir(parents=True)
        for name in ("upload-autonomous-buddy.sh", "ota-config.sh", "ota-metadata.sh"):
            shutil.copyfile(RELEASE_DIR / name, release / name)
        self.script = release / "upload-autonomous-buddy.sh"
        self.buddy = self.root / "integrations/companions/autonomous-buddy"
        (self.buddy / "dist").mkdir(parents=True)
        self.version = self.buddy / "VERSION_AUTONOMOUS_BUDDY"
        self.version.write_text("0.0.19\n")
        self.remote = self.root / "remote.json"
        self.published = self.root / "published.json"
        self.calls = self.root / "calls.log"
        self.remote.write_text(json.dumps({"web": {"version": "1.2.3"}}))
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        self.write_executable(bin_dir / "gsutil", r'''#!/usr/bin/env bash
set -euo pipefail
printf 'gsutil %s\n' "$*" >> "$TEST_CALLS"
while [[ "$1" == "-h" ]]; do shift 2; done
[[ "$1" == "cp" ]] || exit 91
if [[ "$2" == gs://*/metadata.json ]]; then
  [[ "${TEST_FETCH_FAIL:-0}" == "0" ]] || exit 92
  cp "$TEST_REMOTE" "$3"
elif [[ "$3" == gs://*/metadata.json ]]; then
  cp "$2" "$TEST_PUBLISHED"
elif [[ "$3" != gs://*/*.dmg ]]; then
  exit 93
fi
''')
        self.write_executable(bin_dir / "make", r'''#!/usr/bin/env bash
set -euo pipefail
printf 'make %s\n' "$*" >> "$TEST_CALLS"
version=$(tr -d '[:space:]' < VERSION_AUTONOMOUS_BUDDY)
archs="${2#BUDDY_ARCHS=}"
for arch in $archs; do
  printf '%s' "$arch" > "dist/Autonomous-Buddy-${version}-${arch}.dmg"
done
''')
        self.write_executable(bin_dir / "xcrun", r'''#!/usr/bin/env bash
set -euo pipefail
printf 'xcrun %s\n' "$*" >> "$TEST_CALLS"
case "$1 $2" in
  'notarytool history') exit "${TEST_NOTARY_FAIL:-0}" ;;
  'stapler validate') exit "${TEST_STAPLER_FAIL:-0}" ;;
  *) exit 94 ;;
esac
''')
        self.write_executable(bin_dir / "spctl", r'''#!/usr/bin/env bash
set -euo pipefail
printf 'spctl %s\n' "$*" >> "$TEST_CALLS"
exit "${TEST_SPCTL_FAIL:-0}"
''')
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("BUDDY_", "OTA_", "GCS_", "TEST_", "NOTARY_"))}
        self.env.update(PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                        GCS_BUCKET="test-bucket", BUCKET_PREFIX="os",
                        NOTARY_PROFILE="test-profile",
                        TEST_CALLS=str(self.calls), TEST_REMOTE=str(self.remote),
                        TEST_PUBLISHED=str(self.published))

    @staticmethod
    def write_executable(path, content):
        path.write_text(content)
        path.chmod(0o755)

    def run_upload(self, success=True, **overrides):
        result = subprocess.run(["bash", str(self.script)], env=self.env | overrides,
                                capture_output=True, text=True, timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def artifact(self, arch):
        (self.buddy / "dist" / f"Autonomous-Buddy-0.0.19-{arch}.dmg").write_text(arch)

    def test_default_builds_and_publishes_both_architectures(self):
        self.run_upload()
        self.assertEqual(self.version.read_text().strip(), "0.0.20")
        data = json.loads(self.published.read_text())
        self.assertEqual(data["web"], {"version": "1.2.3"})
        self.assertEqual(set(data["autonomous-buddy"]), {"arm64", "x64"})
        for arch in ("arm64", "x64"):
            entry = data["autonomous-buddy"][arch]
            self.assertEqual(entry["version"], "0.0.20")
            self.assertEqual(entry["url"],
                             f"https://storage.googleapis.com/test-bucket/os/ota/autonomous-buddy/{arch}/0.0.20.dmg")
            self.assertEqual(entry["sha256"], hashlib.sha256(arch.encode()).hexdigest())
            self.assertTrue(entry["updated_at"])
        calls = self.calls.read_text()
        self.assertIn("make dmg-signed BUDDY_ARCHS=arm64 x64", calls)
        self.assertIn("xcrun notarytool history --keychain-profile test-profile", calls)
        self.assertEqual(calls.count("xcrun stapler validate"), 2)
        self.assertEqual(calls.count("spctl --assess"), 2)

    def test_single_architecture_preserves_other_lane_and_components(self):
        previous = {"version": "0.0.18", "url": "https://example.test/arm64.dmg",
                    "sha256": "a" * 64, "updated_at": "previous"}
        unrelated = {"version": "3.2.1", "url": "https://example.test/legacy.zip"}
        self.remote.write_text(json.dumps({
            "autonomous-buddy": {"arm64": previous, "version": "0.0.18",
                                 "url": "https://example.test/ambiguous.dmg"},
            "claude-desktop-buddy": unrelated,
        }))
        self.artifact("x64")
        self.run_upload(BUDDY_SKIP_BUILD="1", BUDDY_ARCHS="x64")
        data = json.loads(self.published.read_text())
        self.assertEqual(data["autonomous-buddy"]["arm64"], previous)
        self.assertEqual(data["autonomous-buddy"]["x64"]["version"], "0.0.19")
        self.assertEqual(set(data["autonomous-buddy"]), {"arm64", "x64"})
        self.assertEqual(data["claude-desktop-buddy"], unrelated)
        self.assertEqual(self.version.read_text().strip(), "0.0.19")

    def test_missing_second_artifact_prevents_any_upload(self):
        self.artifact("arm64")
        result = self.run_upload(False, BUDDY_SKIP_BUILD="1")
        self.assertIn("expected DMG not found", result.stderr)
        self.assertNotIn("gsutil", self.calls.read_text())
        self.assertFalse(self.published.exists())

    def test_first_intel_release_migrates_legacy_apple_silicon_release(self):
        previous = {"version": "0.0.18", "url": "https://example.test/old.dmg",
                    "updated_at": "previous"}
        self.remote.write_text(json.dumps({"autonomous-buddy": previous}))
        self.artifact("x64")
        self.run_upload(BUDDY_SKIP_BUILD="1", BUDDY_ARCHS="x64")
        data = json.loads(self.published.read_text())["autonomous-buddy"]
        self.assertEqual(data["arm64"], previous)
        self.assertEqual(data["x64"]["version"], "0.0.19")
        self.assertEqual(set(data), {"arm64", "x64"})

    def test_bare_signed_feed_cannot_be_downgraded_without_key(self):
        payload = base64.b64encode(self.remote.read_bytes()).decode()
        self.remote.write_text(json.dumps({
            "format": "autonomous-ota/v1", "payload": payload,
            "signature": {"algorithm": "ed25519", "value": "fixture"},
        }))
        self.artifact("x64")
        result = self.run_upload(False, BUDDY_SKIP_BUILD="1", BUDDY_ARCHS="x64")
        self.assertIn("signing key required", result.stderr)
        self.assertFalse(self.published.exists())

    def test_fetch_failure_does_not_overwrite_metadata(self):
        for arch in ("arm64", "x64"):
            self.artifact(arch)
        self.run_upload(False, BUDDY_SKIP_BUILD="1", TEST_FETCH_FAIL="1")
        self.assertTrue(self.calls.exists())
        self.assertFalse(self.published.exists())

    def test_invalid_architecture_does_not_bump_or_build(self):
        result = self.run_upload(False, BUDDY_ARCHS="arm64 intel")
        self.assertIn("unsupported architecture", result.stderr)
        self.assertEqual(self.version.read_text().strip(), "0.0.19")
        self.assertFalse(self.calls.exists())

    def test_missing_notary_profile_does_not_bump_or_build(self):
        result = self.run_upload(False, NOTARY_PROFILE="")
        self.assertIn("NOTARY_PROFILE", result.stderr)
        self.assertEqual(self.version.read_text().strip(), "0.0.19")
        self.assertFalse(self.calls.exists())

    def test_invalid_notary_credentials_do_not_bump_or_build(self):
        self.run_upload(False, TEST_NOTARY_FAIL="1")
        self.assertEqual(self.version.read_text().strip(), "0.0.19")
        calls = self.calls.read_text()
        self.assertIn("xcrun notarytool history", calls)
        self.assertNotIn("make ", calls)
        self.assertNotIn("gsutil", calls)

    def test_retry_rejects_unstapled_or_gatekeeper_rejected_artifact(self):
        for failure in ("TEST_STAPLER_FAIL", "TEST_SPCTL_FAIL"):
            with self.subTest(failure=failure):
                self.artifact("x64")
                self.run_upload(False, BUDDY_SKIP_BUILD="1", BUDDY_ARCHS="x64",
                                **{failure: "1"})
                self.assertNotIn("gsutil", self.calls.read_text())
                self.assertFalse(self.published.exists())

    def test_explicit_local_dmg_target_does_not_require_notarization(self):
        self.run_upload(BUDDY_DMG_TARGET="dmg", NOTARY_PROFILE="",
                        TEST_NOTARY_FAIL="1", TEST_STAPLER_FAIL="1", TEST_SPCTL_FAIL="1")
        calls = self.calls.read_text()
        self.assertIn("make dmg BUDDY_ARCHS=arm64 x64", calls)
        self.assertNotIn("xcrun", calls)
        self.assertNotIn("spctl", calls)
        self.assertTrue(self.published.exists())


if __name__ == "__main__":
    unittest.main()
