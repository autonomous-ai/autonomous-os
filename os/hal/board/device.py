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

import logging
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger("hal.device")

# The DEVICE.md `schema:` is an ABI tag (DEVICE-SPEC.md §Versioning): within a
# major version fields are only added, so a v1 file must keep booting on every
# later v1 runtime. The runtime declares which majors it understands; a file
# whose major is unknown can't be parsed safely, so boot fails loud.
SCHEMA_NAMESPACE = "autonomous.device"
SUPPORTED_SCHEMA_MAJORS = frozenset({1})

_RE_SCHEMA = re.compile(r"^schema:\s*(\S+)\s*$", re.MULTILINE)
_RE_SCHEMA_VERSION = re.compile(r"^" + re.escape(SCHEMA_NAMESPACE) + r"\.v(\d+)$")


@dataclass(frozen=True)
class Capability:
    group: str
    routes: List[str]
    required: bool
    safety: Optional[str] = None


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


def _parse_safety(body: str) -> Optional[str]:
    m = re.search(r"safety:\s*([^\s,}]+)", body)
    return m.group(1) if m else None


def validate_schema(front_matter: str) -> str:
    """Parse and validate the `schema:` ABI tag. Returns the raw schema string.

    Raises ValueError if it is missing, malformed, or declares a major version
    this runtime does not support — all of which are deploy faults that must
    fail boot rather than mount a body against an ABI we can't read.
    """
    m = _RE_SCHEMA.search(front_matter)
    if not m:
        raise ValueError(
            f"DEVICE.md is missing the required 'schema:' field "
            f"(expected '{SCHEMA_NAMESPACE}.v<major>')"
        )
    schema = m.group(1)
    v = _RE_SCHEMA_VERSION.match(schema)
    if not v:
        raise ValueError(
            f"DEVICE.md schema '{schema}' is not a valid '{SCHEMA_NAMESPACE}.v<major>' tag"
        )
    major = int(v.group(1))
    if major not in SUPPORTED_SCHEMA_MAJORS:
        raise ValueError(
            f"DEVICE.md schema '{schema}' has major v{major}; this runtime supports "
            f"majors {sorted(SUPPORTED_SCHEMA_MAJORS)}"
        )
    return schema


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
            safety=_parse_safety(body),
        )
    return caps


@dataclass(frozen=True)
class DeviceProfile:
    device_type: str
    schema: str
    capabilities: Dict[str, Capability]

    def declared_routes(self) -> Dict[str, bool]:
        """route -> required. A route is required if ANY capability that
        declares it is required."""
        out: Dict[str, bool] = {}
        for cap in self.capabilities.values():
            for route in cap.routes:
                out[route] = out.get(route, False) or cap.required
        return out


def parse_device(device_type: str, text: str) -> DeviceProfile:
    front_matter = extract_front_matter(text)
    schema = validate_schema(front_matter)  # fail loud on missing/unknown ABI
    return DeviceProfile(
        device_type=device_type,
        schema=schema,
        capabilities=parse_capabilities(front_matter),
    )


def validate_safety_refs(profile: DeviceProfile, safety_md_text: str) -> List[str]:
    """Pure check that each capability's `safety: SAFETY.md#<anchor>` reference
    resolves to a heading in SAFETY.md. Returns human-readable problem strings
    (empty = clean). No file IO so it stays unit-testable.

    The safety enforcement engine does not exist yet; this only catches
    declaration errors, so callers WARN rather than fail boot.
    """
    problems: List[str] = []
    for cap in profile.capabilities.values():
        if not cap.safety:
            continue
        if not safety_md_text:
            problems.append(
                f"capability '{cap.group}' declares safety '{cap.safety}' but SAFETY.md is empty or missing"
            )
            continue
        m = re.match(r"SAFETY\.md#(.+)$", cap.safety)
        if not m:
            continue  # not a SAFETY.md anchor reference; nothing to resolve here
        anchor = m.group(1)
        heading = re.compile(r"^##\s+" + re.escape(anchor) + r"\s*$", re.IGNORECASE | re.MULTILINE)
        if not heading.search(safety_md_text):
            problems.append(
                f"capability '{cap.group}' references '{cap.safety}' but no '## {anchor}' heading found in SAFETY.md"
            )
    return problems


def load_device(device_type: str, devices_dir: str) -> DeviceProfile:
    """Load devices/<device_type>/DEVICE.md from a devices directory."""
    device_dir = os.path.join(devices_dir, device_type)
    with open(os.path.join(device_dir, "DEVICE.md"), "r") as f:
        profile = parse_device(device_type, f.read())

    if any(cap.safety for cap in profile.capabilities.values()):
        safety_path = os.path.join(device_dir, "SAFETY.md")
        safety_text = ""
        if os.path.exists(safety_path):
            with open(safety_path, "r") as f:
                safety_text = f.read()
        else:
            logger.warning(
                "[device] %s declares safety refs but %s is missing", device_type, safety_path
            )
        for problem in validate_safety_refs(profile, safety_text):
            logger.warning("[device] %s: %s", device_type, problem)

    return profile


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
