"""Make the captured `look` frame visible in the Flow Monitor.

The realtime `look` tool saves its frame into the agent workspace so a delegate
turn can hand it over by path. That location is not servable by the monitor, and
nothing referenced the frame in the turn the user actually sees — so a visual
question showed text and no picture.

This copies the frame somewhere the monitor can serve it and hands back a
`[snapshot: ...]` marker for the turn message. Two contracts have to line up:

  * the file must live under /var/lib/hal/snapshots/<category>/<name> — that is
    what `GET /api/sensing/snapshot/:category/:name` serves;
  * the category must start with `sensing_`, because the UI only recognises
    markers matching sensing_/emotion_/motion_ when building thumbnails.

The marker itself never reaches the model: os-server strips `[snapshot: ...]`
from the outgoing message but keeps it in the flow JSONL, which is exactly how
motion.activity surfaces its snapshots.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from typing import Optional

import hal.config as config

logger = logging.getLogger(__name__)

# Must begin with "sensing_" or the monitor will not build a thumbnail for it.
MONITOR_CATEGORY: str = "sensing_look"
# Keep only the most recent few — one frame per visual question adds up, and
# nothing reads the older ones.
KEEP_LAST: int = 20


def _monitor_dir() -> str:
    root = getattr(config, "SNAPSHOT_PERSIST_DIR", "/var/lib/hal/snapshots")
    return os.path.join(root, MONITOR_CATEGORY)


def _prune(directory: str) -> None:
    try:
        files = sorted(
            (f for f in os.listdir(directory) if f.endswith(".jpg")),
            reverse=True,
        )
        for stale in files[KEEP_LAST:]:
            try:
                os.unlink(os.path.join(directory, stale))
            except OSError:
                pass
    except Exception as e:
        logger.debug("[look-monitor] prune skipped: %s", e)


def persist_for_monitor(src_path: Optional[str]) -> Optional[str]:
    """Copy the look frame somewhere the monitor can serve it.

    Returns the servable path, or None. Best-effort throughout: a missing
    thumbnail must never cost the user their answer.
    """
    if not src_path or not os.path.exists(src_path):
        return None
    try:
        directory = _monitor_dir()
        os.makedirs(directory, exist_ok=True)
        dst = os.path.join(directory, f"{int(time.time() * 1000)}.jpg")
        shutil.copyfile(src_path, dst)
        _prune(directory)
        return dst
    except Exception as e:
        logger.debug("[look-monitor] persist failed: %s", e)
        return None


def snapshot_marker(monitor_path: Optional[str]) -> str:
    """The marker to append to a turn message, or "" when there is no frame."""
    return f"[snapshot: {monitor_path}]" if monitor_path else ""
