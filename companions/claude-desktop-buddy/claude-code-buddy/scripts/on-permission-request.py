#!/usr/bin/env python3
"""PermissionRequest hook: voice-approve Claude Code tool prompts on the device.

Fires exactly when Claude Code would show a tool permission dialog. Instead of
the dialog, this hook asks the connected device — the on-device agent reads the
request out loud, the user answers "yes/no", and the decision comes back here so
Claude Code approves/denies WITHOUT showing the dialog.

Opt-in: only runs when `approval_enabled` is true in the config (it changes how
Claude Code prompts, so it's off by default).

Contract (code.claude.com/docs hooks reference):
  stdin : {"hook_event_name":"PermissionRequest","tool_name":..,"tool_input":..}
  stdout: {"hookSpecificOutput":{"hookEventName":"PermissionRequest",
                                  "decision":{"behavior":"allow"|"deny"}}}
  exit 0 (JSON only honored on exit 0).

FAIL-SAFE: on disabled / no device / unreachable / timeout / any error, print
NOTHING and exit 0 — Claude Code falls back to its normal dialog. NEVER prints
"allow" on an error path.
"""

import json
import sys
import uuid

# The hook is launched with this script's dir on sys.path, so the sibling
# shared module imports directly.
from buddy_client import default_device, load_config, request_approval

# Sit between the device long-poll ttl (55s) and the hook `timeout` in hooks.json
# (60s): give the server a moment to return its own "timeout" decision first, but
# still return cleanly before Claude Code kills the hook.
REQUEST_TIMEOUT = 58


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}

    cfg = load_config()
    if not cfg.get("approval_enabled", False):
        return  # opt-in only

    dev = default_device(cfg)
    if not dev:
        return  # no device -> native dialog

    decision = request_approval({
        "id": str(uuid.uuid4()),
        "tool": event.get("tool_name", "?"),
        "input": event.get("tool_input", {}),
    }, dev, timeout=REQUEST_TIMEOUT)

    if decision not in ("allow", "deny"):
        return  # timeout / unreachable / error -> native dialog

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": decision},
        }
    }))


if __name__ == "__main__":
    main()
