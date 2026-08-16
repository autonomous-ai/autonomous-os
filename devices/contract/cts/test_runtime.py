"""Runtime CTS — the COMPATIBILITY.md rules only a live device can answer.

`test_compatibility.py` reads `ROBOT.md` and proves a device is *described*
correctly. It cannot prove the running device matches that description: a
declaration passes the static suite whether or not any hardware ever booted it.
This module closes that gap — it points at a provisioned device and compares
what the device REPORTS MOUNTED against what its `ROBOT.md` DECLARES.

Opt-in, so CI (which has no hardware) stays green:

    CTS_HAL=http://lamp-ac82.local:5001 \\
    CTS_OS=http://lamp-ac82.local:5000 \\
      python3 -m unittest discover -s devices/contract/cts -v

Environment:
    CTS_HAL              HAL base URL. Unset → every test here skips.
    CTS_OS               os-server base URL. Unset → the envelope rule skips.
    CTS_TIMEOUT          per-request timeout in seconds (default 5).
    CTS_STOP_BUDGET_MS   assert the deterministic stop returns within N ms.
                         Unset → the latency is measured and printed, not
                         asserted (COMPATIBILITY.md says "immediate" but names
                         no number; see README §Not covered).
    CTS_ALLOW_MOTION     set to 1 to also exercise stops that MOVE hardware.
                         Off by default: `/servo/release` cuts torque, which
                         drops a raised arm.

Dependency-free on purpose (stdlib urllib): the suite must run on a bare
python3 the same way CI runs the static half, and on a third party's machine
that has only cloned the repo.
"""
import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))  # devices/contract/cts → repo root
sys.path.insert(0, ROOT)  # the hal package lives at the repo root

from hal.board.device import load_device  # noqa: E402  (path set above)

# Where the declarations live. Defaults to this checkout, but is overridable so
# the suite can run ON the device itself — where HAL is already on loopback (no
# port-forward needed) and the profile sits at /opt/devices/<type>:
#   PYTHONPATH=/opt CTS_DEVICES_DIR=/opt/devices CTS_HAL=http://127.0.0.1:5001 \
#     python3 -m unittest test_runtime -v
DEVICES_DIR = os.environ.get("CTS_DEVICES_DIR") or os.path.join(ROOT, "devices")

HAL = (os.environ.get("CTS_HAL") or "").rstrip("/")
OS_SERVER = (os.environ.get("CTS_OS") or "").rstrip("/")
TIMEOUT = float(os.environ.get("CTS_TIMEOUT") or 5)
STOP_BUDGET_MS = os.environ.get("CTS_STOP_BUDGET_MS")
ALLOW_MOTION = os.environ.get("CTS_ALLOW_MOTION") == "1"

# Routes the HAL mounts regardless of declaration — they import cheaply and
# other code calls into them in-process (hal/server.py `_ALWAYS_ROUTES`).
# MUST NOT 16 ("mount a capability its ROBOT.md does not declare") is checked
# against declared ∪ these, or every device would fail on `system`.
ALWAYS_ROUTES = {"audio", "emotion", "scene", "system", "bluetooth"}

# Read-only probe per route: "mounted" is what the device claims, this is the
# route actually answering. Paths are the real GET handlers in hal/routes/*.py
# (absolute — only the bluetooth router carries a prefix). Routes absent from
# this map are not probed; see README §Not covered.
ROUTE_PROBES = {
    "servo": "/servo",
    "led": "/led",
    "camera": "/camera",
    "audio": "/audio",
    "sensing": "/sensing",
    "scene": "/scene",
    "display": "/display",
    "emotion": "/emotion/status",
    "voice": "/voice/status",
    "music": "/audio/status",
}


def _request(url, method="GET", timeout=None):
    """Return (http_status, parsed_json_or_None, elapsed_ms).

    Never raises: a 4xx/5xx is a result the assertions read, and an unreachable
    host becomes status 0 so the operator gets a compliance verdict rather than
    a urllib traceback (a typo'd CTS_HAL is the common case)."""
    req = urllib.request.Request(url, method=method)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    except (urllib.error.URLError, OSError):
        return 0, None, (time.perf_counter() - started) * 1000
    elapsed_ms = (time.perf_counter() - started) * 1000
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        body = None
    return status, body, elapsed_ms


@unittest.skipUnless(HAL, "set CTS_HAL=http://<device>:5001 to run the runtime CTS")
class TestRuntimeConformance(unittest.TestCase):
    """Live-device half of the CTS. Every assertion names the COMPATIBILITY.md
    rule it enforces, so a failure report reads as a compliance verdict."""

    @classmethod
    def setUpClass(cls):
        status, body, _ = _request(f"{HAL}/device")
        cls.probe_status = status
        cls.device = body if isinstance(body, dict) else {}
        cls.reachable = status == 200 and isinstance(body, dict)
        cls.mounted = set(cls.device.get("routes") or [])
        cls.profile = None
        dev_id = cls.device.get("id")
        if dev_id and os.path.isfile(os.path.join(DEVICES_DIR, str(dev_id), "ROBOT.md")):
            cls.profile = load_device(str(dev_id), DEVICES_DIR)

    def setUp(self):
        # One clear failure (test_device_is_reachable) instead of the same
        # connection error repeated across every rule below.
        if not self.reachable and self.id().rsplit(".", 1)[-1] != "test_device_is_reachable":
            self.skipTest(f"device at {HAL} not reachable — see test_device_is_reachable")

    def test_device_is_reachable(self):
        """Fails loud rather than skipping: the operator asked for a device run
        by setting CTS_HAL, so a silent green here would be a false pass."""
        self.assertTrue(
            self.reachable,
            f"GET {HAL}/device returned {self.probe_status or 'no response'} — the device is "
            f"unreachable, or is not running an Autonomous HAL. Nothing below could be verified.")

    def _profile(self):
        """The repo-side declaration for the device under test, or skip. A
        device whose id has no folder here is running a fork's declaration —
        the runtime rules still hold, but this checkout cannot verify them."""
        if self.profile is None:
            self.skipTest(
                f"device reports id={self.device.get('id')!r}, which has no devices/<id>/ROBOT.md "
                f"in this checkout — cannot compare running device against its declaration")
        return self.profile

    # ── MUST 1 — ships a ROBOT.md with schema, a stable id, a type, its boards ──

    def test_must1_identity_is_served(self):
        for field in ("id", "name", "type", "schema", "board"):
            self.assertTrue(self.device.get(field),
                            f"GET /device omits '{field}' — MUST 1 identity is not observable at runtime")
        self.assertEqual(self.device["schema"], "autonomous.device.v1",
                         "MUST 1: running device does not report schema autonomous.device.v1")

    def test_must1_running_board_is_declared(self):
        profile = self._profile()
        board = self.device.get("board")
        self.assertIn(board, profile.boards,
                      f"MUST 1: device booted on board {board!r}, which ROBOT.md does not list "
                      f"({profile.boards}) — the declaration does not describe this hardware")

    def test_must1_reported_id_matches_declaration(self):
        profile = self._profile()
        self.assertEqual(self.device.get("id"), profile.id,
                         "MUST 1: the id the device serves differs from the one its ROBOT.md declares")

    # ── MUST 5 — every declared `required` capability is up, or the boot failed loud ──

    def test_must5_required_routes_are_mounted(self):
        profile = self._profile()
        required = {r for r, req in profile.declared_routes().items() if req}
        missing = sorted(required - self.mounted)
        self.assertFalse(missing,
                         f"MUST 5: {missing} are declared required but are not mounted, yet the device "
                         f"is serving requests — this is the silent half-boot the rule forbids")

    def test_must5_mounted_routes_answer(self):
        """Mounted is a claim; answering is the fact. Probes a read-only GET on
        every mounted route this suite knows an endpoint for."""
        unanswered = []
        for route in sorted(self.mounted):
            path = ROUTE_PROBES.get(route)
            if not path:
                continue
            status, _, _ = _request(f"{HAL}{path}")
            if status >= 500:
                unanswered.append(f"{route} ({path} → {status})")
        self.assertFalse(unanswered,
                         f"MUST 5: mounted routes that do not answer: {unanswered}")

    # ── MUST 2 / 3 — system capability, and a primary sense or output, at runtime ──

    def test_must2_system_capability_is_live(self):
        status, _, _ = _request(f"{HAL}/health")
        self.assertEqual(status, 200,
                         "MUST 2: the 'system' capability must answer — GET /health did not return 200")

    def test_must3_primary_sense_is_mounted(self):
        profile = self._profile()
        groups = set(profile.capabilities)
        primary = {"audio", "vision"} & groups
        self.assertTrue(primary, "MUST 3: device declares no primary sense/output")
        routes = {r for g in primary for r in profile.capabilities[g].routes}
        self.assertTrue(routes & self.mounted,
                        f"MUST 3: declares {sorted(primary)} but mounted none of its routes {sorted(routes)}")

    # ── MUST NOT 16 — mount a capability its ROBOT.md does not declare ──

    def test_mustnot16_no_undeclared_route_is_mounted(self):
        profile = self._profile()
        declared = set(profile.declared_routes()) | ALWAYS_ROUTES
        undeclared = sorted(self.mounted - declared)
        self.assertFalse(undeclared,
                         f"MUST NOT 16: {undeclared} are mounted but not declared in ROBOT.md — "
                         f"a skill could reach hardware the device never advertised")

    # ── MUST NOT 15 / MUST 6 — a motion device ships a deterministic stop ──

    def test_mustnot15_motion_device_has_deterministic_stop(self):
        profile = self._profile()
        if "motion" not in profile.capabilities:
            self.skipTest("device declares no motion capability")
        status, _, elapsed_ms = _request(f"{HAL}/servo/track/stop", method="POST")
        self.assertLess(status, 400,
                        f"MUST NOT 15: device declares motion but POST /servo/track/stop returned {status} — "
                        f"no deterministic stop reachable")
        print(f"\n    [cts] deterministic stop answered in {elapsed_ms:.0f} ms", end="")
        if STOP_BUDGET_MS:
            self.assertLessEqual(elapsed_ms, float(STOP_BUDGET_MS),
                                 f"MUST 6: stop took {elapsed_ms:.0f} ms, over the "
                                 f"{STOP_BUDGET_MS} ms budget given in CTS_STOP_BUDGET_MS")

    def test_mustnot15_torque_off_is_reachable(self):
        """`/servo/release` is the hard stop (torque off). It DROPS a raised
        arm, so it only runs when the operator opts in."""
        profile = self._profile()
        if "motion" not in profile.capabilities:
            self.skipTest("device declares no motion capability")
        if not ALLOW_MOTION:
            self.skipTest("set CTS_ALLOW_MOTION=1 to exercise torque-off — it drops a raised arm")
        status, _, elapsed_ms = _request(f"{HAL}/servo/release", method="POST")
        self.assertLess(status, 400,
                        f"MUST NOT 15: POST /servo/release returned {status} — torque-off unreachable")
        print(f"\n    [cts] torque-off answered in {elapsed_ms:.0f} ms", end="")

    # ── MUST 7 — the standard API envelope ──

    @unittest.skipUnless(OS_SERVER, "set CTS_OS=http://<device>:5000 to check the API envelope")
    def test_must7_success_envelope(self):
        status, body, _ = _request(f"{OS_SERVER}/api/health/live")
        self.assertEqual(status, 200, f"GET /api/health/live returned {status}")
        self.assertIsInstance(body, dict, "MUST 7: response body is not a JSON object")
        self.assertEqual(set(body), {"status", "data", "message"},
                         f"MUST 7: envelope keys are {sorted(body)}, expected status/data/message")
        self.assertEqual(body["status"], 1, "MUST 7: a successful call must report status 1")
        self.assertIsNone(body["message"], "MUST 7: message must be null on success")


if __name__ == "__main__":
    unittest.main()
