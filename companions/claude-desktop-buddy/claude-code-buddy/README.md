# Claude Code Buddy

Push Claude Code activity to your device on the same network. When Claude finishes a task, needs your input, or your usage runs high, the device lets you know through its own feedback — LED, round display, or voice — so you don't have to watch the terminal.

The plugin POSTs a few simple events to a companion daemon on the device (HTTP, port 5002). The device decides how to react; the plugin never touches the hardware. This is the Claude-Code/HTTP counterpart to the existing Claude-Desktop/Bluetooth path into the same device.

## Quick Start

```bash
claude plugins marketplace add https://raw.githubusercontent.com/autonomous-ai/autonomous-os/main/companions/claude-desktop-buddy/claude-code-buddy/.claude-plugin/marketplace.json
claude plugins install claude-code-buddy
```

Restart Claude Code, then connect your device:

```
connect my device
```

Claude finds the device on your LAN and saves it to your config. See the full [Setup Guide](GUIDE.md) for details.

## Features

- **Task Done push** — when Claude completes a task, the device signals you're free to look back.
- **High-usage push** — when your 5-hour or 7-day usage crosses your threshold, the device surfaces your current usage so you can pace yourself.
- **"Claude needs you" ping** — when Claude is waiting on your approval or your input has gone idle, the device pings you with a distinct cue so you don't miss it from across the room.
- **Custom notifications** — send a one-off message to your device ("notify my device when the build passes").
- **mDNS discovery** — finds the device automatically, no codes to type.
- **Zero dependencies** — Python 3 stdlib only, no pip install.

## Commands

| Command | Description |
|---------|-------------|
| `/claude-code-buddy:usage` | Push current usage to the device now |
| `/claude-code-buddy:notify` | Send a notification to the device |

Or use natural language: "push my usage to my device", "notify my device", "connect my device", "mute my device", "warn me earlier".

## Turning notifications on/off

You don't always want a sound or every ping. Just tell Claude in plain language — it edits `~/.config/claude-code-buddy.json` for you (no restart needed):

- "mute my device" → keep the events, silence the sound
- "stop the task done notification" → no more Task Done push
- "stop pinging me when Claude needs me" → no more "Claude needs you" ping
- "turn everything back on" → re-enable all

The flags (all default `true`) plus the threshold:

| Key | Controls |
|-----|----------|
| `sounds_enabled` | Whether events carry a sound cue. `false` = silent feedback |
| `task_done_enabled` | The Task Done push after each response |
| `notify_enabled` | The "Claude needs you" ping (approval / idle) |
| `usage_threshold` | Usage % (0–100) that triggers a high-usage push. Default `80` |

## Requirements

- macOS, Python 3 (stdlib only — nothing to `pip install`)
- A **device** (e.g. a lamp), set up and on the **same LAN** as your Mac
- Its companion service (`claude-desktop-buddy`) running on the device — this exposes the `:5002` API the plugin talks to
- Claude Code signed in with **OAuth** (Pro/Max login, not `ANTHROPIC_API_KEY`)

## Status

The plugin sends events today. The device-side receivers (`/claude-code/notify`, `/claude-code/usage`) exist and log each push, but the device-side rendering (the HAL bridge to LED / display / voice) is still being rolled out, so depending on your device firmware those events may not produce feedback yet. Discovery and config work regardless.

## Update / Uninstall

```bash
claude plugins update claude-code-buddy@claude-code-buddy   # pull latest
claude plugins uninstall claude-code-buddy                  # remove plugin
```
