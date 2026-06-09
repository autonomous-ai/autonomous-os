---
name: turn-gate
description: "Sets Lamp agent busy state immediately when a channel message is received, before lifecycle_start SSE arrives"
homepage: https://github.com/autonomous-ai/lamp
metadata:
  { "openclaw": {
      "emoji": "🚦",
      "events": ["message:preprocessed"],
      "requires": { "bins": ["node"] }
    }
  }
---

# turn-gate

Fires at `message:preprocessed` — before the LLM turn begins — and signals Lamp server to set agent busy immediately.

This closes the timing gap for channel-initiated turns (Telegram, Slack, Discord) that bypass Lamp server entirely. Without this hook, passive sensing events can slip through between the time OpenClaw starts processing and when lifecycle_start SSE arrives at Lamp (~50ms later).

## Behavior

- Skips `[sensing:*]` messages — those are Lamp-originated, busy state is already set proactively in sendChat
- Skips empty messages
- Skips OpenClaw heartbeat turns by body content (`bodyForAgent` contains literal `HEARTBEAT_OK` from `HEARTBEAT_PROMPT`) — these never emit `lifecycle.end` SSE, so setting busy=true would wedge Lamp for the full 5-min `busyTTL` (see `docs/debug/busy-stuck.md`)
- Calls `POST http://127.0.0.1:5000/api/openclaw/busy` on Lamp server
- Fails silently — never blocks message delivery
