#!/usr/bin/env python3
"""Notification hook: ping the device when Claude needs the user.

Fires on Claude Code's `Notification` event — i.e. when Claude needs approval
to run a tool (a yes/no prompt) or the input has been left idle. Handy when
you've wandered off and forgot to hit enter.

Rate-limited to once per 8s so a burst of prompts doesn't machine-gun the
device.
"""

import json
import os
import re
import sys
import time

# The hook is launched with this script's dir on sys.path, so the sibling
# shared module imports directly.
from buddy_client import default_device, load_config, send

COOLDOWN_PATH = os.path.expanduser("~/.config/claude-code-buddy-notify.last")
COOLDOWN_SECONDS = 8

PERMISSION_RE = re.compile(r"permission to use (.+?)[.\s]*$", re.IGNORECASE)


def should_run():
    try:
        last = float(open(COOLDOWN_PATH).read().strip())
        return (time.time() - last) >= COOLDOWN_SECONDS
    except Exception:
        return True


def mark_ran():
    try:
        with open(COOLDOWN_PATH, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def pretty_tool(name):
    """Shorten tool names for the device.

    MCP tools arrive as 'mcp__<server>__<tool>' which is far too long — keep
    just the last '__' segment with underscores spaced out (e.g.
    'mcp__claude_ai_Google_Drive__authenticate' -> 'authenticate').
    """
    name = name.strip()
    if name.startswith("mcp__"):
        name = name.split("__")[-1] or name
    return name.replace("_", " ").strip()


def parse_message(message):
    """Return (title, subtitle) — a clean headline + one short line."""
    msg = (message or "").strip()
    low = msg.lower()
    m = PERMISSION_RE.search(msg)
    if m:
        # "Claude needs your permission to use Bash" -> ("Approve?", "Bash")
        return "Approve?", pretty_tool(m.group(1))
    if "permission" in low or "approve" in low or "allow" in low:
        return "Approve?", "needs your ok"
    if "waiting" in low or "idle" in low or "input" in low:
        return "Your turn", "waiting for you"
    return "Heads up", msg


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}

    if not should_run():
        sys.exit(0)

    cfg = load_config()
    if not cfg.get("notify_enabled", True):
        sys.exit(0)

    dev = default_device(cfg)
    if not dev:
        sys.exit(0)

    title, subtitle = parse_message(event.get("message", "Claude needs you"))

    # Mark the cycle as ran before sending (same as on-stop-done): the cooldown
    # throttles the ping regardless of whether the device is reachable.
    mark_ran()
    send("/claude-code/notify", {
        "title": title,
        "subtitle": subtitle,
        "level": "attention",
        "sound": cfg.get("sounds_enabled", True),
    }, dev)


if __name__ == "__main__":
    main()
