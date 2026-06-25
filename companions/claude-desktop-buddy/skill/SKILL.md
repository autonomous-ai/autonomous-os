---
name: claude-desktop-buddy
description: Coordinate with Claude Desktop Buddy plugin for approval prompts and state awareness
---

# Claude Desktop Buddy

Lamp is connected to Claude Desktop on the user's Mac via Bluetooth.
A buddy-plugin runs on this device and syncs Desktop state to Lamp's LED/display.

## When you receive a `[sensing:buddy_approval]` event

Claude Desktop is waiting for the user to approve or deny a tool call.

**Workflow:**
1. Express emotion: curious (intensity 0.8)
2. Read the approval details from the event message
3. Ask the user naturally: mention the tool name and what it affects
4. Wait for the user's verbal response

**If user says approve/yes/ok/go ahead:**
```bash
curl -s -X POST http://127.0.0.1:5002/claude-desktop/approve \
  -H "Content-Type: application/json" \
  -d '{"id": "<prompt_id from event>"}'
```

**If user says deny/no/skip/cancel:**
```bash
curl -s -X POST http://127.0.0.1:5002/claude-desktop/deny \
  -H "Content-Type: application/json" \
  -d '{"id": "<prompt_id from event>"}'
```

## Buddy state awareness

You can check what Claude Desktop is doing:
```bash
curl -s http://127.0.0.1:5002/status
```

Response:
```json
{
  "state": "busy",
  "connected": true,
  "sessions_running": 2,
  "tokens_today": 8200,
  "pending_prompt": null
}
```

## Rules

- When buddy state is `attention`: do NOT start ambient behaviors or proactive conversations — the user is being prompted for approval
- When buddy state is `busy`: the user is actively using Claude Desktop — reduce proactive interruptions (no wellbeing reminders, no music suggestions)
- When buddy state is `idle` or `sleep`: operate normally
- NEVER mention "buddy-plugin", "BLE", "Bluetooth", or technical internals to the user — just say "Claude Desktop" naturally
