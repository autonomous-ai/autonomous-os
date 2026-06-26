# Claude Code Plugin (`claude-code-buddy/`)

The on-device daemon already bridges **Claude Desktop** (Mac) to the device over
**Bluetooth LE** — heartbeats, chat events, and permission prompts come in over a
Nordic UART link and become LED / display / voice feedback (see
[`architecture.md`](architecture.md)).

The sibling **Claude Code plugin** at [`../claude-code-buddy/`](../claude-code-buddy/)
is the **Claude Code / HTTP** counterpart to that BLE path. Instead of pairing over
Bluetooth, it runs on the user's Mac as a Claude Code plugin and **PUSHes** Claude
Code activity to the device's HTTP API on `:5002`. The daemon is meant to translate
those pushes into the same device feedback (LED / display / voice) it already
produces for the BLE feed.

## How it complements the BLE path

```
 Mac                                Device (Pi / OrangePi)
 ┌────────────────────┐  BLE/NUS   ┌──────────────────────────────────────┐
 │  Claude Desktop     │ ─────────►│  claude-desktop-buddy daemon          │
 │  (Hardware Buddy)   │           │   BLE ─► state machine ─► bridge ─┐    │
 └────────────────────┘           │                                   ▼    │
                                   │                          device feedback│
 ┌────────────────────┐  HTTP      │   httpapi/ :5002 ────────────────┘     │
 │  Claude Code        │  POST     │   /claude-code/*    (notify/usage)     │
 │  (claude-code-buddy)│ ─────────►│   /status /health /claude-desktop/*    │
 └────────────────────┘  :5002     └──────────────────────────────────────┘
```

Both paths feed the **same** device behavior. The BLE path is a continuous,
bonded link driven by Claude Desktop; the HTTP path is a set of one-shot pushes
driven by Claude Code hooks. The plugin never touches the hardware directly — it
only POSTs events, and the device decides how to react.

## The `:5002` push contract

The plugin sends JSON to `http://<device>:5002`. The daemon replies `{"ok":true}`
on success.

### `POST /claude-code/notify`

A discrete signal: Claude finished, needs you, or a custom message.

```json
{
  "title": "Claude is done",
  "subtitle": "auth refactor",
  "level": "done",
  "sound": true
}
```

| Field | Type | Notes |
|-------|------|-------|
| `title` | string | Headline shown / spoken on the device |
| `subtitle` | string | Optional secondary line |
| `level` | string | One of `"done"`, `"attention"`, `"info"` — selects the feedback cue |
| `sound` | bool | Whether the device adds an audible cue |

```bash
curl -s -X POST http://my-device.local:5002/claude-code/notify \
  -H 'Content-Type: application/json' \
  -d '{"title":"Claude is done","subtitle":"auth refactor","level":"done","sound":true}'
```

### `POST /claude-code/usage`

Current Claude Code usage, pushed when it crosses your threshold (or on demand).

```json
{
  "five_hour": 72,
  "seven_day": 40,
  "reset_5h": "3:00 PM",
  "reset_7d": "Mon",
  "sound": false
}
```

| Field | Type | Notes |
|-------|------|-------|
| `five_hour` | int | 5-hour usage percentage (0–100) |
| `seven_day` | int | 7-day usage percentage (0–100) |
| `reset_5h` | string | When the 5-hour window resets |
| `reset_7d` | string | When the 7-day window resets |
| `sound` | bool | Whether the device adds an audible cue |

```bash
curl -s -X POST http://my-device.local:5002/claude-code/usage \
  -H 'Content-Type: application/json' \
  -d '{"five_hour":72,"seven_day":40,"reset_5h":"3:00 PM","reset_7d":"Mon","sound":false}'
```

### Existing daemon endpoints

These are served by the daemon's `httpapi/` package and used by the BLE/approval
flow: `GET /status`, `GET /health`, `POST /claude-desktop/approve`,
`POST /claude-desktop/deny`. See
[`architecture.md`](architecture.md#the-approval-round-trip).

## Voice-approve (Claude Code reverse approval)

When Claude Code on the Mac would show a **tool-permission dialog**, a hook can ask
the connected device instead: the on-device agent reads the request out loud, the
user answers **yes/no by voice**, and the decision flows back so Claude Code
approves or denies the tool **without ever showing the dialog**. This is the
**HTTP mirror** of the existing Claude Desktop BLE approval round-trip
(see [`architecture.md`](architecture.md#the-approval-round-trip)).

The new capability that makes this possible is a Claude Code **`PermissionRequest`
hook**. It fires only when a permission dialog *would* show, blocks synchronously
(up to its 60 s timeout), and returns a decision over stdout as JSON:

```json
{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}}
```

(exit `0`, `"behavior"` is `"allow"` or `"deny"`). It does **not** fire in
`-p` / headless mode, nor for tools already permitted by your permission rules.

### The round-trip

```
 Mac (Claude Code)                         Device (Pi / OrangePi)
 ┌──────────────────────┐                  ┌────────────────────────────────────┐
 │ tool needs permission│                  │  claude-code-buddy daemon :5002     │
 │        │             │  POST            │                                    │
 │        ▼             │  approval-request│  register pending ──► cue device:  │
 │ PermissionRequest    │ ────────────────►│    • blink LED (transient)         │
 │ hook (blocks ≤60s)   │  (long-poll)     │    • round display "Approve <tool>?"│
 │        ▲             │                  │    • sensing event → OS server     │
 │        │ {decision}  │ ◄────────────────│  block until resolved / ttl        │
 └────────┼─────────────┘                  │            ▲                       │
          │                                │  approve/deny (loopback) ◄─ agent  │
   no dialog, proceed                      └────────────────────────────────────┘
```

1. Claude Code → `PermissionRequest` hook `scripts/on-permission-request.py`
   (registered in the plugin's `hooks/hooks.json`, matcher `*`, timeout 60 s).
2. The hook POSTs to the device daemon
   `POST http://<device>:5002/claude-code/approval-request {id, tool, input}` and
   **blocks** (long-poll).
3. The daemon registers the pending approval, cues the device — a transient blink
   LED, a round-display **"Approve `<tool>`?"**, and a sensing event
   `type=claude_code_approval` emitted to the OS server `/api/sensing/event` — then
   blocks until the request is resolved or the long-poll TTL elapses.
4. The on-device OpenClaw agent hears the sensing event (a skill in
   [`skill/SKILL.md`](../skill/SKILL.md)) and asks the user; on **"yes"** it calls
   `POST 127.0.0.1:5002/claude-code/approve {id}`, on **"no"**
   `POST 127.0.0.1:5002/claude-code/deny {id}`.
5. The daemon unblocks the long-poll → returns `{"decision":"allow"|"deny"}` → the
   hook prints the `PermissionRequest` decision JSON → Claude Code proceeds with
   **no dialog**.

### Voice-approve endpoints (`:5002`)

| Endpoint | Body | Behavior |
|----------|------|----------|
| `POST /claude-code/approval-request` | `{id, tool, input, hint?}` | Long-polls; returns `{"decision":"allow"｜"deny"｜"timeout"}`. `400` if `id` is missing. |
| `POST /claude-code/approve` | `{id}` | Resolves the pending request as allow. **Loopback-only (`403` otherwise)** — it decides whether code runs on the user's Mac. `409` if `id` is unknown or already resolved. |
| `POST /claude-code/deny` | `{id}` | Same as `approve` but resolves as deny (loopback-only, `403` / `409` as above). |
| `GET /claude-code/pending` | — | `{"pending":[{id,tool,hint}]}` — what's currently awaiting a voice answer. |

### Config

- **Plugin** (`~/.config/claude-code-buddy.json`) — new flag `approval_enabled`
  (bool, default **`false`**). It is **opt-in** because it changes how Claude Code
  prompts you. When `false`, the hook does nothing and the **native dialog** shows.
- **Daemon** (`config/buddy.json`) — `code_approval_ttl_sec` (default **55**): how
  long a request long-polls before returning `"timeout"`.

### Fail-safe

The hook is **fail-open to the native dialog** and **never auto-approves on an
error path**. On any of: feature disabled, no device connected, device
unreachable, timeout, or any other error — the hook prints **nothing** and exits
`0`, so Claude Code shows its **normal** permission dialog.

### Prerequisites / caveats

- **macOS Local Network Privacy** must grant **Local Network** to the app running
  Claude Code (the hook's `python3` must reach the device IP) — the same gate as
  the outbound push (see [below](#troubleshooting-macos-local-network)).
- **Headless `-p` mode:** `PermissionRequest` does not fire, so voice-approve is
  out of scope there.

## Discovery and config

- **Discovery** — the plugin finds the device via **mDNS** service type
  `_autonomous._tcp` (the same advertiser the device already runs), so there are
  no codes to type. The resolved address + port `:5002` is what the pushes target.
- **Config (Mac)** — saved at `~/.config/claude-code-buddy.json`. It records the
  device address and the plugin's notify/usage/sound preferences. The plugin edits
  this file in response to plain-language requests ("mute my device", "warn me
  earlier"); no restart is required.

## Plugin pieces

| Piece | Role |
|-------|------|
| **Hooks** | `Stop` → `POST /claude-code/notify` (`level":"done"`) plus a `POST /claude-code/usage`; `Notification` → `POST /claude-code/notify` (`level":"attention"`) |
| `scripts/buddy_client.py` | Minimal HTTP client that performs the POSTs (Python 3 stdlib only) |
| `scripts/discover.py` | mDNS resolver for `_autonomous._tcp` |
| Command `/claude-code-buddy:usage` | Push current usage to the device now |
| Command `/claude-code-buddy:notify` | Send a one-off notification to the device |

## Install / usage

From Claude Code on the Mac:

```bash
claude plugins marketplace add https://raw.githubusercontent.com/autonomous-ai/autonomous-os/main/companions/claude-desktop-buddy/claude-code-buddy/.claude-plugin/marketplace.json
claude plugins install claude-code-buddy
```

Restart Claude Code, then connect the device (mDNS finds it on the LAN):

```
connect my device
```

After that, the `Stop` and `Notification` hooks push automatically, and you can
push on demand with `/claude-code-buddy:usage` or `/claude-code-buddy:notify` (or
plain language: "push my usage to my device", "notify my device"). The full plugin
setup guide lives at [`../claude-code-buddy/GUIDE.md`](../claude-code-buddy/GUIDE.md).

## Troubleshooting: macOS Local Network

Recent macOS (Sonoma / Sequoia) blocks apps from reaching local-network devices
until they are granted **Local Network** permission. The plugin runs under
`python3`, so without that permission Python cannot reach the device — even
though the device is online and reachable from `curl`.

**Symptoms:** `connect my device` can't find the device; and after connecting,
nothing arrives on it (no Task Done / usage / ping) because the hooks' network
calls are silently blocked.

**Fix:** open **System Settings → Privacy & Security → Local Network** and enable
it for the app that runs Claude Code (Terminal / iTerm / the Claude app), then
restart that app.

Confirm Python can reach the device (use your device's IP):

```bash
python3 -c "import urllib.request as u; print(u.urlopen('http://192.168.1.50:5002/health', timeout=2).read())"
```

A `{"status":"ok",...}` response means it works. `No route to host` from Python
while `curl` to the same address succeeds is the tell-tale sign the permission is
still off.

## Status / remaining work

> **The daemon-side endpoints `POST /claude-code/notify` and
> `POST /claude-code/usage` now exist.** They accept the pushes described above,
> **log** the payload, and return `{"ok":true}`. What is still **not** done is
> bridging those logged events to the device's actual feedback — there is no HAL
> bridge yet, so the pushes do not produce LED / round-display / voice output. The
> remaining work is the device-side rendering (wiring the logged payloads through
> HAL). Discovery (mDNS) and the plugin's Mac-side config work regardless.
