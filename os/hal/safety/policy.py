"""Safety policy layer — read a device's SAFETY.md bounds and expose pure,
deterministic gate functions the HAL routes/drivers call before actuating.

Mirrors os/hal/board/device.py: a dependency-free regex front-matter parser (no
pyyaml in the runtime) and pure functions, fully unit-testable off-hardware. This
is the mechanism behind the first principle in contract/SAFETY-SPEC.md — *safety
is below the brain*: the gate sits in the request path between the agent and the
hardware, runs on every request regardless of who issued it, and cannot be
bypassed by prompting.

Enforced bounds:
  - slice 1: light.max_brightness            — LED brightness ceiling
  - slice 2: light.quiet_hours{max_brightness}, audio.quiet_hours — a nightly
             window that lowers the LED ceiling and suppresses loud audio (music)
  - slice 3: motion.max_speed                — deg/s ceiling, enforced by
             stretching a move's duration (see min_move_duration)
  - slice 4: thermal.max_temp_c              — SoC over-temp → health event +
             stop discretionary motion (background monitor, see thermal_over)

Enforcement is presence-driven and uniform across capabilities: a declared bound
is enforced, an absent one is pass-through — the engine never invents a limit
nobody wrote. Removing a section (or the whole front matter) turns its enforcement
off; there is no separate kill switch.

The autonomous.safety.v1 ABI only ever gains fields. See contract/SAFETY-SPEC.md
and docs/safety.md.
"""
from __future__ import annotations

import logging
import os
import re
import urllib.request
from dataclasses import dataclass
from datetime import time as dtime
from typing import Optional, Tuple

from hal.clock import device_now

logger = logging.getLogger("hal.safety")

# The SAFETY.md `schema:` is an ABI tag (SAFETY-SPEC.md §Versioning), identical in
# discipline to autonomous.device.v1: within a major version fields are only added,
# so a v1 file must keep enforcing on every later v1 runtime. A file that declares
# a major this runtime does not understand cannot be parsed safely → fail loud.
SCHEMA_NAMESPACE = "autonomous.safety"
SUPPORTED_SCHEMA_MAJORS = frozenset({1})

_RE_SCHEMA = re.compile(r"^schema:\s*(\S+)\s*$", re.MULTILINE)
_RE_SCHEMA_VERSION = re.compile(r"^" + re.escape(SCHEMA_NAMESPACE) + r"\.v(\d+)$")

MAX_CHANNEL = 255  # 8-bit per-channel RGB ceiling


@dataclass(frozen=True)
class QuietHours:
    """A daily time window (may wrap past midnight). `max_brightness` is the
    reduced LED ceiling that applies inside the window (light only; None for the
    audio window, which suppresses loud output rather than dimming it)."""
    start: dtime
    end: dtime
    max_brightness: Optional[int] = None


@dataclass(frozen=True)
class MotionBounds:
    # deg/s ceiling — enforced by stretching a move's duration so its fastest
    # joint never exceeds it (the move still reaches its target, just not too
    # fast). None = no speed ceiling declared.
    max_speed: Optional[int] = None
    # motion.stop is deterministic and never gated (you must always be able to
    # halt a body — stop/release/zero/hold are recovery actions, never refused).
    stop_always: bool = False


@dataclass(frozen=True)
class ThermalBounds:
    # SoC temperature (°C) at/above which the device is "thermally over": the
    # runtime surfaces a health event and stops discretionary motion (tracking).
    # The threshold is device/SoC-specific — read the board's own critical trip
    # point (`/sys/class/thermal/.../trip_point_*_temp`); never a generic guess.
    max_temp_c: int
    # cooled to/below this clears the over state (hysteresis, avoids flapping at
    # the boundary). Defaults to max_temp_c - 10 when not declared.
    resume_temp_c: int


@dataclass(frozen=True)
class SafetyPolicy:
    schema: str
    # light brightness ceiling (0–255). None = no ceiling declared → pass-through
    # (light fail-safe: a calm LED is not a hazard; never invent a limit).
    max_brightness: Optional[int] = None
    light_quiet: Optional[QuietHours] = None   # nightly reduced LED ceiling
    audio_quiet: Optional[QuietHours] = None    # nightly window: suppress loud audio
    # Motion bounds. None = no machine motion bounds declared → pass-through, the
    # same presence-driven rule as light/audio: a declared bound is enforced, an
    # absent one is not (the engine never invents a limit nobody wrote). The only
    # motion enforcement is the speed cap (see min_move_duration).
    motion: Optional[MotionBounds] = None
    # Thermal bound. None = no SoC over-temp monitoring declared → off (the same
    # presence-driven rule). When set, a background monitor reads SoC temp and, on
    # crossing max_temp_c, raises a health event + stops discretionary motion.
    thermal: Optional[ThermalBounds] = None


def extract_front_matter(text: str) -> str:
    """Return the YAML front-matter block (between the first two '---' fences)."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    return m.group(1) if m else ""


def validate_schema(front_matter: str) -> str:
    """Parse + validate the `schema:` ABI tag. Returns the raw schema string.

    Raises ValueError if missing/malformed/unknown-major — a deploy fault that
    must fail boot rather than enforce a bounds ABI the runtime cannot read.
    """
    m = _RE_SCHEMA.search(front_matter)
    if not m:
        raise ValueError(
            f"SAFETY.md front matter is missing 'schema:' "
            f"(expected '{SCHEMA_NAMESPACE}.v<major>')"
        )
    schema = m.group(1)
    v = _RE_SCHEMA_VERSION.match(schema)
    if not v:
        raise ValueError(
            f"SAFETY.md schema '{schema}' is not a valid '{SCHEMA_NAMESPACE}.v<major>' tag"
        )
    major = int(v.group(1))
    if major not in SUPPORTED_SCHEMA_MAJORS:
        raise ValueError(
            f"SAFETY.md schema '{schema}' has major v{major}; this runtime supports "
            f"majors {sorted(SUPPORTED_SCHEMA_MAJORS)}"
        )
    return schema


def _section_body(front_matter: str, key: str) -> str:
    """Return the body of a top-level `key:` section — either flow style
    (`key: { ... }`) or block style (`key:` then indented lines). '' if absent.
    Scoping by section keeps `max_brightness` under `light` distinct from the one
    inside `light.quiet_hours`."""
    flow = re.search(
        r"^" + re.escape(key) + r":[ \t]*\{(.*?)\}[ \t]*$",
        front_matter, re.MULTILINE | re.DOTALL,
    )
    if flow:
        return flow.group(1)
    block = re.search(
        r"^" + re.escape(key) + r":[ \t]*\n((?:[ \t]+.*\n?)*)",
        front_matter, re.MULTILINE,
    )
    return block.group(1) if block else ""


def _int_field(body: str, name: str) -> Optional[int]:
    m = re.search(r"\b" + re.escape(name) + r":\s*(\d+)", body)
    return int(m.group(1)) if m else None


def _validate_brightness(val: Optional[int], where: str) -> Optional[int]:
    if val is not None and not (0 <= val <= MAX_CHANNEL):
        raise ValueError(f"SAFETY.md {where} {val} out of range 0–{MAX_CHANNEL}")
    return val


def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    hi, mi = int(h), int(m)
    if not (0 <= hi <= 23 and 0 <= mi <= 59):
        raise ValueError(f"SAFETY.md quiet_hours time '{s}' is not a valid HH:MM")
    return dtime(hour=hi, minute=mi)


def _parse_quiet_hours(section_body: str, *, with_brightness: bool) -> Optional[QuietHours]:
    """Parse a `quiet_hours: { start: "HH:MM", end: "HH:MM"[, max_brightness: N] }`
    object out of a section body, or None if absent."""
    m = re.search(r"quiet_hours:\s*\{([^}]*)\}", section_body)
    if not m:
        return None
    body = m.group(1)
    ms = re.search(r'start:\s*"?(\d{1,2}:\d{2})"?', body)
    me = re.search(r'end:\s*"?(\d{1,2}:\d{2})"?', body)
    if not ms or not me:
        raise ValueError("SAFETY.md quiet_hours requires both 'start' and 'end' (HH:MM)")
    mb = _validate_brightness(_int_field(body, "max_brightness"), "quiet_hours.max_brightness") if with_brightness else None
    return QuietHours(start=_parse_hhmm(ms.group(1)), end=_parse_hhmm(me.group(1)), max_brightness=mb)


def _parse_motion(motion_body: str) -> Optional[MotionBounds]:
    """Parse the `motion:` section into MotionBounds, or None if it declares no
    real bounds (an absent/empty section is pass-through, like light/audio)."""
    max_speed = _int_field(motion_body, "max_speed")
    if max_speed is not None and max_speed <= 0:
        raise ValueError(f"SAFETY.md motion.max_speed {max_speed} must be > 0 (deg/s)")
    stop_always = bool(re.search(r"\bstop_always:\s*true\b", motion_body))
    if max_speed is None and not stop_always:
        return None
    return MotionBounds(max_speed=max_speed, stop_always=stop_always)


def _parse_thermal(thermal_body: str) -> Optional[ThermalBounds]:
    """Parse the `thermal:` section into ThermalBounds, or None if it declares no
    `max_temp_c` (absent → no thermal monitoring, like every other bound)."""
    max_temp = _int_field(thermal_body, "max_temp_c")
    if max_temp is None:
        return None
    if max_temp <= 0:
        raise ValueError(f"SAFETY.md thermal.max_temp_c {max_temp} must be > 0 (°C)")
    resume = _int_field(thermal_body, "resume_temp_c")
    if resume is None:
        resume = max_temp - 10
    if resume >= max_temp:
        raise ValueError(
            f"SAFETY.md thermal.resume_temp_c {resume} must be < max_temp_c {max_temp}"
        )
    return ThermalBounds(max_temp_c=max_temp, resume_temp_c=resume)


def parse_safety(text: str) -> SafetyPolicy:
    """Parse SAFETY.md text (which HAS front matter) into a SafetyPolicy.
    Validates the schema fail-loud; raises on an out-of-range/malformed bound."""
    fm = extract_front_matter(text)
    schema = validate_schema(fm)
    # Drop full-line comments so commented-out placeholders (e.g. `# stop_always:
    # true`) are not mistaken for declared bounds. Inline trailing comments stay
    # (the int/time regexes stop at the value).
    fm = "\n".join(ln for ln in fm.splitlines() if not ln.lstrip().startswith("#"))
    light_body = _section_body(fm, "light")
    audio_body = _section_body(fm, "audio")
    # base light ceiling = max_brightness outside the quiet_hours object
    light_base_body = re.sub(r"quiet_hours:\s*\{[^}]*\}", "", light_body)
    return SafetyPolicy(
        schema=schema,
        max_brightness=_validate_brightness(_int_field(light_base_body, "max_brightness"), "light.max_brightness"),
        light_quiet=_parse_quiet_hours(light_body, with_brightness=True),
        audio_quiet=_parse_quiet_hours(audio_body, with_brightness=False),
        motion=_parse_motion(_section_body(fm, "motion")),
        thermal=_parse_thermal(_section_body(fm, "thermal")),
    )


# ── time helpers ─────────────────────────────────────────────────────────────

def _now() -> dtime:
    """Device-local wall-clock time. Isolated so gates stay unit-testable: callers
    pass an explicit `now` in tests; production reads the clock here.

    Reads the device's CURRENT timezone each call (see hal.clock) so quiet-hours
    gates stay correct after a runtime timezone change without restarting HAL."""
    return device_now().time()


def in_window(window: QuietHours, now: dtime) -> bool:
    """True if `now` falls inside the window, handling wrap past midnight
    (start > end, e.g. 22:00→07:00 = late evening OR early morning)."""
    if window.start <= window.end:
        return window.start <= now < window.end
    return now >= window.start or now < window.end


# ── gate functions — pure when `now` is passed, deterministic, single point ──────

def active_max_brightness(policy: Optional[SafetyPolicy], now: Optional[dtime] = None) -> Optional[int]:
    """The LED brightness ceiling in effect right now: the base ceiling, lowered
    to the quiet-hours ceiling while inside the light quiet window. None = no
    ceiling (pass-through)."""
    if policy is None:
        return None
    if now is None:
        now = _now()
    base = policy.max_brightness
    q = policy.light_quiet
    if q is not None and q.max_brightness is not None and in_window(q, now):
        return q.max_brightness if base is None else min(base, q.max_brightness)
    return base


def clamp_brightness(policy: Optional[SafetyPolicy], value: int, now: Optional[dtime] = None) -> int:
    """Clamp a 0–255 brightness scalar to the ceiling in effect now. No ceiling →
    pass-through unchanged (light fail-safe)."""
    ceiling = active_max_brightness(policy, now)
    return value if ceiling is None else min(value, ceiling)


def clamp_color(
    policy: Optional[SafetyPolicy], color: Tuple[int, int, int], now: Optional[dtime] = None
) -> Tuple[int, int, int]:
    """Scale an (r,g,b) tuple so its brightest channel respects the ceiling in
    effect now, preserving hue (full white 255, ceiling 180 → 180,180,180; pure
    red → 180,0,0). Pass-through when no ceiling or already within it."""
    ceiling = active_max_brightness(policy, now)
    if ceiling is None:
        return color
    r, g, b = color
    peak = max(r, g, b)
    if peak <= ceiling:
        return color
    scale = ceiling / peak
    return (round(r * scale), round(g * scale), round(b * scale))


def audio_quiet_now(policy: Optional[SafetyPolicy], now: Optional[dtime] = None) -> bool:
    """True if loud discretionary audio (music) must be suppressed right now —
    i.e. inside the declared audio quiet-hours window."""
    if policy is None or policy.audio_quiet is None:
        return False
    if now is None:
        now = _now()
    return in_window(policy.audio_quiet, now)


def min_move_duration(
    policy: Optional[SafetyPolicy],
    target: dict,
    current: dict,
    requested: float,
) -> float:
    """The duration to actually use for a move: at least `requested`, but stretched
    so the fastest joint stays within motion.max_speed (deg/s). The move still
    reaches `target` — only its speed is capped, never its destination. Pure;
    pass-through (returns `requested`) when no speed ceiling is declared.

    target/current: {joint: degrees}. Joints absent from `current` are ignored
    (no known start → can't bound their speed here)."""
    if policy is None or policy.motion is None or policy.motion.max_speed is None:
        return requested
    max_delta = 0.0
    for joint, tgt in target.items():
        cur = current.get(joint)
        if cur is None:
            continue
        max_delta = max(max_delta, abs(float(tgt) - float(cur)))
    needed = max_delta / policy.motion.max_speed
    return max(requested, needed)


def thermal_over(policy: Optional[SafetyPolicy], temp_c: Optional[float], was_over: bool) -> bool:
    """Hysteresis gate for SoC over-temperature. Returns whether the device should
    be in the thermal-over state given the current temp and the previous state:

      - no policy / no thermal bound / unreadable temp → False (monitoring off).
      - not yet over: trip True once temp reaches `max_temp_c`.
      - already over: stay True until temp cools to/below `resume_temp_c`.

    Pure (no IO) so the state machine is unit-testable; the caller supplies temp."""
    if policy is None or policy.thermal is None or temp_c is None:
        return False
    t = policy.thermal
    if was_over:
        return temp_c > t.resume_temp_c
    return temp_c >= t.max_temp_c


_THERMAL_ZONE = "/sys/class/thermal/thermal_zone0/temp"


def read_soc_temp_c(path: str = _THERMAL_ZONE) -> Optional[float]:
    """Read the SoC temperature in °C from the kernel thermal zone (millidegrees),
    or None if unreadable (no zone, permission, parse error) — best-effort, never
    raises. Isolated like _now() so the gate stays pure in tests."""
    try:
        with open(path, "r") as f:
            return int(f.read().strip()) / 1000.0
    except Exception:
        return None


# ── loader ───────────────────────────────────────────────────────────────────

def _read_ref(device_dir: str, ref: str) -> str:
    """Resolve a *_ref to text, mirroring device._read_ref: an http(s) URL is
    downloaded, anything else is read as a path relative to the device dir."""
    if ref.startswith("http://") or ref.startswith("https://"):
        with urllib.request.urlopen(ref, timeout=30) as r:  # noqa: S310 (device-trusted ref)
            return r.read().decode("utf-8")
    with open(os.path.join(device_dir, ref), "r") as f:
        return f.read()


def load_safety(device_dir: str, safety_ref: str) -> Optional[SafetyPolicy]:
    """Resolve `safety_ref` (path/URL) and parse the bounds, or None when there
    are no enforceable bounds (pass-through; light fail-safe):

      - no safety_ref                          → None
      - safety_ref set but file unreadable     → None + WARN (declared-but-absent)
      - SAFETY.md present but no front matter   → None + WARN (legacy prose-only)

    A SAFETY.md that *does* carry front matter must have a valid schema — a
    missing/malformed/unknown-major tag (or an out-of-range bound) raises and
    aborts boot, since the runtime will not enforce an ABI it cannot read
    (contract/SAFETY-SPEC.md).
    """
    if not safety_ref:
        return None
    try:
        text = _read_ref(device_dir, safety_ref)
    except Exception as e:
        logger.warning(
            "[safety] cannot read safety_ref %r: %s — bounds not enforced", safety_ref, e
        )
        return None
    if not extract_front_matter(text):
        logger.warning(
            "[safety] %s has no machine front matter — bounds not enforced (prose only)",
            safety_ref,
        )
        return None
    return parse_safety(text)  # validates schema fail-loud
