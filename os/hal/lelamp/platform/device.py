"""Device profile layer — read a device's DEVICE.md and turn its declared
capabilities into a mount plan for the HAL runtime.

This replaces the implicit `try/except ImportError` route-skip in server.py,
which could not tell three different situations apart. The declaration makes
them explicit:

  - declared + driver present       -> mount
  - declared + required + missing   -> FAIL LOUD (a hardware fault)
  - declared + optional + missing   -> skip (graceful degradation)
  - undeclared                      -> skip (a different device, by design)

Dependency-free: a focused parser for the DEVICE.md front-matter capability
block (no pyyaml in the runtime). Pure functions so the logic is unit-testable
off-hardware. See contract/DEVICE-SPEC.md and contract/capabilities.md.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Capability:
    group: str
    routes: List[str]
    required: bool


def extract_front_matter(text: str) -> str:
    """Return the YAML front-matter block (between the first two '---' fences)."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    return m.group(1) if m else ""


def _parse_routes(body: str) -> List[str]:
    m = re.search(r"routes:\s*\[([^\]]*)\]", body)
    if not m:
        return []
    return [r.strip() for r in m.group(1).split(",") if r.strip()]


def _parse_required(body: str) -> bool:
    m = re.search(r"required:\s*(true|false)", body, re.IGNORECASE)
    return bool(m and m.group(1).lower() == "true")


def parse_capabilities(front_matter: str) -> Dict[str, Capability]:
    """Parse the `capabilities:` block of a DEVICE.md front matter.

    Supports the flow-style entries this repo uses, e.g.:
        capabilities:
          audio:  { routes: [audio, speaker, voice], required: true }
          motion: { routes: [servo], driver: feetech, required: false }
    """
    caps: Dict[str, Capability] = {}
    in_block = False
    block_indent: Optional[int] = None
    for line in front_matter.splitlines():
        if re.match(r"^capabilities:\s*$", line):
            in_block = True
            continue
        if not in_block:
            continue
        if line.strip() == "":
            continue
        indent = len(line) - len(line.lstrip())
        if block_indent is None:
            block_indent = indent
        if indent < block_indent:        # dedented back to a top-level key -> block ended
            break
        m = re.match(r"^\s+([A-Za-z0-9_]+):\s*\{(.*)\}\s*$", line)
        if not m:
            continue
        group, body = m.group(1), m.group(2)
        caps[group] = Capability(
            group=group,
            routes=_parse_routes(body),
            required=_parse_required(body),
        )
    return caps


@dataclass(frozen=True)
class DeviceProfile:
    device_id: str
    capabilities: Dict[str, Capability]

    def declared_routes(self) -> Dict[str, bool]:
        """route -> required. A route is required if ANY capability that
        declares it is required."""
        out: Dict[str, bool] = {}
        for cap in self.capabilities.values():
            for route in cap.routes:
                out[route] = out.get(route, False) or cap.required
        return out


def parse_device(device_id: str, text: str) -> DeviceProfile:
    return DeviceProfile(device_id=device_id, capabilities=parse_capabilities(extract_front_matter(text)))


def load_device(device_id: str, devices_dir: str) -> DeviceProfile:
    """Load devices/<device_id>/DEVICE.md from a devices directory."""
    with open(os.path.join(devices_dir, device_id, "DEVICE.md"), "r") as f:
        return parse_device(device_id, f.read())


@dataclass(frozen=True)
class MountPlan:
    mounted: List[str]
    skipped: List[str]           # undeclared, or declared-optional-but-absent
    failed_required: List[str]   # declared + required + absent -> caller must raise

    @property
    def ok(self) -> bool:
        return not self.failed_required


def plan_mounts(declared: Dict[str, bool], available: Dict[str, bool]) -> MountPlan:
    """Pure mount planner — the heart of declaration-driven mounting.

    declared:  route -> required   (from DEVICE.md)
    available: route -> driver importable/initialized
    """
    mounted: List[str] = []
    skipped: List[str] = []
    failed: List[str] = []
    for route in sorted(set(declared) | set(available)):
        if route not in declared:
            skipped.append(route)               # not this device
        elif available.get(route, False):
            mounted.append(route)               # declared + present
        elif declared[route]:
            failed.append(route)                # declared + required + missing -> loud
        else:
            skipped.append(route)               # declared + optional + missing -> graceful
    return MountPlan(mounted=mounted, skipped=skipped, failed_required=failed)
