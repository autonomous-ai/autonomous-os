"""Exercise the OTA nginx migration against isolated configuration fixtures."""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "software-update"


class NginxMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.nginx = self.root / "nginx"
        for directory in ("conf.d", "sites-enabled", "sites-available"):
            (self.nginx / directory).mkdir(parents=True)
        self.events = self.root / "events"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("nginx", "systemctl"):
            stub = self.bin / name
            stub.write_text(
                '#!/bin/sh\nprintf "%s\\n" "' + name + ' $*" >> "$EVENTS"\n'
            )
            stub.chmod(0o755)

    def migrate(self):
        match = re.search(
            r"^ensure_harness_nginx_location\(\) \{\n.*?^\}$",
            SCRIPT.read_text(),
            flags=re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(match, "Missing nginx migration function")
        function = match.group(0).replace("/etc/nginx", str(self.nginx))
        return subprocess.run(
            ["/bin/bash", "-c", "set -e\n" + function
             + "\nensure_harness_nginx_location\n"],
            env={**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                 "EVENTS": str(self.events)},
            capture_output=True,
            text=True,
        )

    def recorded_events(self):
        return self.events.read_text().splitlines() if self.events.exists() else []

    @staticmethod
    def configuration(target):
        return (
            "server {\n"
            "  listen 80;\n"
            "  location / {\n"
            "    proxy_pass http://unrelated_ui;\n"
            "  }\n"
            "  location /api/ {\n"
            f"    proxy_pass {target};\n"
            "    proxy_set_header Host $host;\n"
            "  }\n"
            "}\n"
        )

    def assert_route(self, content, target):
        route = re.search(r"location = /api/harness/ws\s*\{([^}]+)\}", content)
        self.assertIsNotNone(route, content)
        self.assertIn(f"proxy_pass {target};", route.group(1))
        self.assertIn("proxy_set_header Upgrade $http_upgrade;", route.group(1))

    def test_reachy_site_preserves_symlink_and_uses_its_api_upstream(self):
        target = self.nginx / "sites-available" / "reachy-spike"
        target.write_text(self.configuration("http://spike_backend"))
        target.chmod(0o640)
        enabled = self.nginx / "sites-enabled" / "reachy-spike"
        enabled.symlink_to("../sites-available/reachy-spike")

        result = self.migrate()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(enabled.is_symlink())
        self.assertEqual(os.readlink(enabled), "../sites-available/reachy-spike")
        self.assert_route(target.read_text(), "http://spike_backend")
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.recorded_events(), ["nginx -t", "systemctl reload nginx"])

        content = target.read_text()
        result = self.migrate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(enabled.is_symlink())
        self.assertEqual(target.read_text(), content)
        self.assertEqual(content.count("location = /api/harness/ws"), 1)
        self.assertEqual(self.recorded_events().count("systemctl reload nginx"), 1)

    def test_standard_configs_reuse_named_or_direct_api_targets(self):
        for filename, target in (
            ("conf.d/os.conf", "http://backend"),
            ("sites-enabled/default", "http://127.0.0.1:8080"),
        ):
            with self.subTest(filename=filename, target=target):
                conf = self.nginx / filename
                conf.write_text(self.configuration(target))
                result = self.migrate()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_route(conf.read_text(), target)
                conf.unlink()

    def test_missing_or_unsupported_api_proxy_leaves_config_unchanged(self):
        for directive in (
            "return 404;",
            "proxy_pass http://backend/api/;",
            "proxy_pass http://$backend;",
        ):
            with self.subTest(directive=directive):
                conf = self.nginx / "sites-enabled" / "reachy-spike"
                content = self.configuration("http://backend").replace(
                    "proxy_pass http://backend;", directive
                )
                conf.write_text(content)
                result = self.migrate()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(conf.read_text(), content)
                self.assertEqual(self.recorded_events(), [])


if __name__ == "__main__":
    unittest.main()
