"""OpenClaw-layout long-term memory must reach realtime instructions."""

from hal.realtime.context_manager.openclaw import OpenClawContextManager


def test_load_device_memory_includes_root_memory_and_recent_daily_memory(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "MEMORY.md").write_text("The user prefers tea.", encoding="utf-8")
    memory_dir = workspace / "memory"
    memory_dir.mkdir()
    (memory_dir / "2026-08-21.md").write_text("Asked about calendars.", encoding="utf-8")

    manager = OpenClawContextManager(
        workspace_dir=str(workspace),
        realtime_memory_path=str(tmp_path / "realtime" / "memory.jsonl"),
    )

    assert manager.load_device_memory() == [
        "## Long-term memory\n\nThe user prefers tea.",
        "## 2026-08-21\n\nAsked about calendars.",
    ]
