"""Exercise the OpenClaw updater without network, package, or service changes."""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "software-update"


def shell_function(name):
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{\n.*?^\}}$",
        SCRIPT.read_text(),
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"Missing shell function: {name}")
    return match.group(0)


MOCKS = r'''
event() { printf '%s\n' "$*" >> "$EVENTS"; }
npm() {
  event "npm $*"
  if [ "$1" = view ]; then
    [ "$FAIL" != metadata ] || return 1
    case "$FAIL" in
      null_metadata) printf '%s\n' 'null' ;;
      empty_metadata) printf '%s\n' '""' ;;
      *) printf '%s\n' '">=24.16.0 <25 || >=26.1.0"' ;;
    esac
  elif [ "$1" = install ]; then
    [ "$FAIL" != install ] || return 1
  fi
}
node_satisfies_engine() {
  event "engine $*"
  [ "$FAIL" != checker ] || return 2
  [ "$SCENARIO" = compatible ] && return 0
  [ "$FAIL" != incompatible ] && [ -f "$UPGRADED" ]
}
node() { printf '%s\n' 'v22.23.1'; }
curl() {
  event "curl $*"
  [ "$FAIL" != download ]
}
bash() {
  event "bash $*"
  [ "$FAIL" != setup ]
}
apt-get() {
  event "apt-get $*"
  [ "$FAIL" != apt ] || return 1
  touch "$UPGRADED"
}
openclaw() { event "openclaw $*"; }
systemctl() { event "systemctl $*"; }
'''


class OpenClawUpdateTests(unittest.TestCase):
    def run_update(self, scenario="incompatible", failure=""):
        functions = "\n".join(
            shell_function(name)
            for name in ("ensure_openclaw_node", "update_openclaw")
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            events = directory / "events"
            result = subprocess.run(
                ["/bin/bash", "-c", "set -e\n" + functions + MOCKS
                 + '\nupdate_openclaw "2026.9.3"\n'],
                env={
                    **os.environ,
                    "EVENTS": str(events),
                    "UPGRADED": str(directory / "upgraded"),
                    "TMPDIR": temporary,
                    "SCENARIO": scenario,
                    "FAIL": failure,
                },
                capture_output=True,
                text=True,
            )
            return result, events.read_text().splitlines() if events.exists() else []

    def test_compatible_node_installs_without_upgrade(self):
        result, events = self.run_update(scenario="compatible")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("npm view openclaw@2026.9.3 engines.node --json", events)
        self.assertIn("npm install -g openclaw@2026.9.3", events)
        self.assertIn("systemctl restart openclaw", events)
        self.assertFalse(any(line.startswith(("curl ", "bash ", "apt-get "))
                             for line in events), events)

    def test_incompatible_node_upgrades_and_rechecks_before_install(self):
        result, events = self.run_update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any("setup_24.x" in line for line in events), events)
        install = events.index("npm install -g openclaw@2026.9.3")
        upgrade = events.index("apt-get install -y nodejs")
        checks = [index for index, line in enumerate(events)
                  if line.startswith("engine ")]
        self.assertEqual(len(checks), 2, events)
        self.assertLess(checks[0], upgrade)
        self.assertLess(upgrade, checks[1])
        self.assertLess(checks[1], install)
        self.assertLess(install, events.index("systemctl restart openclaw"))

    def test_failures_prevent_package_install_and_service_restart(self):
        for failure in ("metadata", "null_metadata", "empty_metadata", "checker",
                        "download", "setup", "apt", "incompatible"):
            with self.subTest(failure=failure):
                result, events = self.run_update(failure=failure)
                self.assertNotEqual(result.returncode, 0, events)
                self.assertFalse(any(line.startswith("npm install ")
                                     for line in events), events)
                self.assertFalse(any(line.startswith("systemctl ")
                                     for line in events), events)
                if failure in ("metadata", "null_metadata", "empty_metadata", "checker"):
                    self.assertFalse(any(line.startswith(("curl ", "bash ", "apt-get "))
                                         for line in events), events)

    def test_package_install_failure_does_not_restart(self):
        result, events = self.run_update(scenario="compatible", failure="install")
        self.assertNotEqual(result.returncode, 0, events)
        self.assertIn("npm install -g openclaw@2026.9.3", events)
        self.assertFalse(any(line.startswith(("systemctl ", "openclaw "))
                             for line in events), events)


if __name__ == "__main__":
    unittest.main()
