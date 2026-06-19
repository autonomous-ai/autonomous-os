"""Device-local wall-clock that reflects the CURRENT system timezone.

HAL is a long-running process and the agent can change the device timezone at
runtime (e.g. `timedatectl set-timezone` → updates `/etc/localtime` and
`/etc/timezone`). A plain `datetime.now()` keeps the timezone glibc cached at
process start, so time-of-day logic (quiet hours, LED dimming, the realtime turn
clock, daily file bucketing) would stay wrong until HAL restarts.

These helpers read `/etc/timezone` fresh on every call via `zoneinfo`, so they
always reflect the current zone — no restart needed. They fall back to naive
local time when `/etc/timezone` is missing (dev/macOS) or unparseable, matching
the device convention used by skills (`export TZ=$(cat /etc/timezone)`).
"""

from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Module-level so tests can point it at a fixture.
_TZ_FILE = Path("/etc/timezone")


def device_timezone() -> Optional[ZoneInfo]:
    """The device's configured zone from /etc/timezone, or None if unavailable
    (caller then falls back to naive system-local time)."""
    try:
        name = _TZ_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def device_now() -> datetime:
    """Current wall-clock as a datetime in the device's configured timezone.
    Returns naive system-local time when the zone can't be resolved."""
    return datetime.now(device_timezone())


def device_fromtimestamp(ts: float) -> datetime:
    """Convert an epoch timestamp to a datetime in the device's configured
    timezone (naive system-local when the zone can't be resolved)."""
    return datetime.fromtimestamp(ts, device_timezone())
