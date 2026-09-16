"""The realtime summary's pending tasks must expire (issues #419 / #421)."""

from pathlib import Path
from unittest.mock import MagicMock

from hal.realtime.context_manager.base import OPEN_REQUESTS_HEADING, expire_open_requests
from hal.realtime.context_manager.openclaw import OpenClawContextManager

SUMMARY = (
    "## Facts\n"
    "- The user prefers Vietnamese\n"
    "\n"
    f"{OPEN_REQUESTS_HEADING}\n"
    "- [2026-09-15T11:56] turn off the TV\n"
    "- [2026-09-15T11:58] find the pen\n"
    "\n"
    "## Mood\n"
    "- Calm\n"
)


def test_fresh_summary_is_untouched():
    assert expire_open_requests(SUMMARY, age_s=120, ttl_s=3600) == SUMMARY


def test_stale_summary_drops_only_the_open_requests_section():
    out = expire_open_requests(SUMMARY, age_s=3601, ttl_s=3600)
    assert "turn off the TV" not in out
    assert "find the pen" not in out
    assert OPEN_REQUESTS_HEADING not in out
    assert "- The user prefers Vietnamese" in out
    assert "## Mood\n- Calm" in out


def test_zero_ttl_disables_expiry():
    assert expire_open_requests(SUMMARY, age_s=10**9, ttl_s=0) == SUMMARY


def test_summary_without_the_section_is_untouched_when_stale():
    plain = "## Facts\n- The user prefers Vietnamese\n"
    assert expire_open_requests(plain, age_s=10**6, ttl_s=3600) == plain


def test_section_at_end_of_summary_is_dropped():
    tail = "## Facts\n- x\n\n" + OPEN_REQUESTS_HEADING + "\n- [t] pending thing\n"
    assert expire_open_requests(tail, age_s=10**6, ttl_s=3600) == "## Facts\n- x"


def test_refeed_fails_open_when_summary_vanishes_between_read_and_stat():
    """POST /api/agent/memory/reset can delete summary.md concurrently — the
    stat() call that follows read_text() must not raise past this helper."""
    manager = object.__new__(OpenClawContextManager)
    fake_path = MagicMock(spec=Path)
    fake_path.read_text.return_value = SUMMARY
    fake_path.stat.side_effect = FileNotFoundError("summary.md vanished")
    manager._summary_path = fake_path
    manager._open_request_ttl_s = 3600

    assert manager._read_summary_for_refeed() == ""
