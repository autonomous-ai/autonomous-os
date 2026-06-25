---
name: claude-code-buddy
description: >
  Push Claude Code activity to the user's device (e.g. a lamp) over
  the LAN. The plugin POSTs semantic events to a companion daemon on the device
  (HTTP, port 5002); the device turns them into its own feedback (LED / round
  display / voice). Supports connecting a device, pushing usage, sending
  notifications, and tuning how/when the user is alerted.
  Triggers: "connect my device", "push my usage to my device", "notify my device",
  "send to my device", "mute my device", "warn me earlier",
  "set warning threshold to N", "stop the task done notification".
allowed-tools: Bash(*)
---

# Claude Code Buddy Skill

Connect to a device on the local network and push Claude Code
events to it. After connecting, the Stop and Notification hooks fire
automatically — this skill covers the on-demand actions (connect, notify,
usage) and the plain-language config tuning.

The plugin only speaks the HTTP contract below. The device decides the actual
LED / display / voice feedback; the plugin never draws anything itself.

## Config

Path: `~/.config/claude-code-buddy.json` (file permission `0600`).

```json
{
  "devices": [
    { "label": "My Device", "host": "lamp-a1b2.local", "last_known_ip": "192.168.1.50" }
  ],
  "default_host": "lamp-a1b2.local",
  "usage_threshold": 80,
  "sounds_enabled": true,
  "task_done_enabled": true,
  "notify_enabled": true
}
```

- `host` is the device's mDNS hostname (like `lamp-a1b2.local`); `last_known_ip`
  is the cached address used to avoid re-resolving every time.
- Shared helpers live in `scripts/buddy_client.py` (config load/save, discovery,
  `send()`, usage fetch). Prefer them over re-implementing.

---

## 1. Discover / Connect

Find the device on the LAN and save it to config.

**When to use:** "connect my device", "set up my device", or when a POST fails
with a connection error/timeout (the cached IP is stale).

### Run discovery

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/discover.py
```

**How it works (automatic):** cache check (cached IP → `/health`) → mDNS browse
for `_autonomous._tcp` → `/24` subnet `/health` sweep. Returns the found
device(s) as JSON, e.g.:

```json
[{ "host": "lamp-a1b2.local", "ip": "192.168.1.50" }]
```

**On success:** write the device into `devices[]` (append new, update existing
by `host`), set `default_host` if it's the first device, and write the file with
permission `0600`.

**On failure:** exits non-zero with `{"error": "not_found"}` on stderr. Suggest:
device powered on? Same WiFi as the computer?

---

## 2. Notify

POST a notification to the device. Most common on-demand action.

**When to use:** "notify my device", "send to my device", "ping my device when
done". Also reasonable proactively at the end of a long task.

### 2.1 Pick the target

Use `default_host` (and its `last_known_ip`) from config. If no config or no
devices → tell the user to connect a device first (section 1).

### 2.2 Build the payload

```json
{
  "title": "Build passed",
  "subtitle": "3m 12s, 0 failures",
  "level": "done",
  "sound": true
}
```

- `title` (string, required) — short headline, plain text.
- `subtitle` (string) — one line of detail.
- `level` — `"done"` | `"attention"` | `"info"`. Use `attention` when the user
  needs to act, `done` for completions, `info` otherwise.
- `sound` (boolean) — whether the event carries a sound cue. The device picks the
  actual cue; do not send sound indices. Respect `sounds_enabled` from config.

### 2.3 Send (urllib, stdlib only)

```python
import json, urllib.request

payload = json.dumps({
    "title": "Build passed",
    "subtitle": "3m 12s",
    "level": "done",
    "sound": True,
}).encode()

req = urllib.request.Request(
    "http://<ip>:5002/claude-code/notify",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=3) as resp:
    print(resp.status)          # expect 200, body {"ok": true}
```

Replace `<ip>` with the device's `last_known_ip` (or resolve `host`).

---

## 3. Usage

Fetch the user's OAuth usage and POST it to `/claude-code/usage`.

**When to use:** "push my usage to my device", "/claude-code-buddy:usage".

### 3.1 Fetch usage

Read the OAuth token (`~/.claude/.credentials.json`, or the macOS keychain item
`Claude Code-credentials`; the token starts with `sk-ant-oat`), then:

```python
import json, urllib.request

req = urllib.request.Request(
    "https://api.anthropic.com/api/oauth/usage",
    headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
    },
)
with urllib.request.urlopen(req, timeout=15) as resp:
    usage = json.loads(resp.read())

from buddy_client import time_left

five_hour = int(usage["five_hour"]["utilization"])
seven_day = int(usage["seven_day"]["utilization"])
reset_5h  = time_left(usage["five_hour"]["resets_at"])   # -> "1h 56m"
reset_7d  = time_left(usage["seven_day"]["resets_at"])   # -> "1d 11h"
```

`scripts/buddy_client.py` already wraps token lookup + fetch — use it.

### 3.2 POST to /claude-code/usage

```json
{
  "five_hour": 41,
  "seven_day": 48,
  "reset_5h": "1h 56m",
  "reset_7d": "1d 11h",
  "sound": true
}
```

`reset_5h` / `reset_7d` are **display-ready** strings (humanize the `resets_at`
ISO timestamp with `buddy_client.time_left()`), not raw ISO — the device shows
them as-is.

POST to `http://<ip>:5002/claude-code/usage` exactly like section 2.3 (expect `200`,
`{"ok": true}`). The usage API rate-limits hard — do not poll faster than once
per minute.

---

## 4. Task Done + "Claude needs you" (automatic)

These run via Claude Code hooks; no skill action needed.

- **Stop hook** (`scripts/on-stop-done.py`) — fires after each response. POSTs
  `/claude-code/notify` with `level: "done"`. If 5-hour or 7-day usage is `>=`
  `usage_threshold`, it also POSTs `/claude-code/usage`. Skipped when
  `task_done_enabled` is `false`.
- **Notification hook** (`scripts/on-notify.py`) — fires when Claude needs
  approval or input has gone idle. POSTs `/claude-code/notify` with
  `level: "attention"`.
  Skipped when `notify_enabled` is `false`.

Both honor `sounds_enabled` for the `sound` field and fail silently if no device
is connected.

---

## 5. Tuning (plain-language config edits)

Let the user retune behavior in plain language — read config, change only the
relevant key(s), preserve everything else, write back with permission `0600`,
then confirm in plain language. If the config is missing → tell them to connect a
device first.

### 5.1 Threshold

**When to use:** "warn me earlier", "set warning threshold to 60", "only warn me
near the limit", "I want pushes at 90%".

- Explicit number → use it. "earlier"/"sooner" → lower (e.g. 60).
  "only near the limit" → raise (e.g. 90). If unclear, ask for a number.
- Validate integer **0–100**, then set `usage_threshold`.

### 5.2 The three toggle flags

All default `true` and live at the top level of the config.

| Key | Controls |
|-----|----------|
| `sounds_enabled` | Whether events carry a sound cue. `false` = silent feedback. |
| `task_done_enabled` | The Task Done push after each response. |
| `notify_enabled` | The "Claude needs you" ping (approval / idle). |

Mapping requests:

- "mute my device" / "turn off the sounds" → `sounds_enabled = false`
- "unmute" / "sounds back on" → `sounds_enabled = true`
- "stop the task done notification" → `task_done_enabled = false`
- "stop pinging me when Claude needs me" → `notify_enabled = false`
- "turn all notifications off" → `task_done_enabled = false` **and** `notify_enabled = false`
- "turn everything back on" → all three `true`

The hooks read these flags on every run, so changes take effect immediately — no
restart.

---

## 6. HTTP responses

| Status | Meaning / Action |
|--------|------------------|
| **200** | Success, body `{"ok": true}`. Update `last_known_ip` in config if it changed. |
| **400** | Bad JSON / malformed payload. Fix the payload and retry. |
| **Connection error / timeout** | Cached IP is stale or device offline. Re-run discovery (section 1) and retry once. |

---

## Important

- Python 3 stdlib only — no pip, no extra dependencies.
- Keep `sound` a boolean; the device decides the actual cue.
- Never print or log the OAuth token.
- Config file permission must be `0600`.
- Device-side `/claude-code/notify` and `/claude-code/usage` receivers exist and
  log the payload, but may not yet render device feedback on every device (the HAL
  bridge is the remaining work); `/health` and `/status` exist for
  liveness/discovery.
