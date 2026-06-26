---
name: claude-buddy
description: Coordinate with the Claude Buddy companion (Claude Desktop + Claude Code on the user's Mac) — voice-approve tool permission prompts and stay aware of Claude activity.
---

# Claude Buddy

The user runs Claude on their Mac (Claude Desktop and/or Claude Code). A companion
daemon on this device bridges them: it surfaces Claude's state and asks you to
approve tool permission prompts out loud, so the user can say "yes" instead of
clicking. The daemon listens on `127.0.0.1:5002`.

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

## When you receive a `[sensing:claude_code_approval]` event

Claude Code (the CLI on the user's Mac) is waiting for the user to approve or deny
a tool call. The Mac is **blocked** on your answer, so respond promptly.

**Workflow:**
1. Express emotion: curious (intensity 0.8)
2. Read the approval details from the event message (tool name + what it affects)
3. Ask the user naturally: mention the tool and what it will do
4. Wait for the user's verbal response

**If user says approve/yes/ok/go ahead:**
```bash
curl -s -X POST http://127.0.0.1:5002/claude-code/approve \
  -H "Content-Type: application/json" \
  -d '{"id": "<prompt_id from event>"}'
```

**If user says deny/no/skip/cancel:**
```bash
curl -s -X POST http://127.0.0.1:5002/claude-code/deny \
  -H "Content-Type: application/json" \
  -d '{"id": "<prompt_id from event>"}'
```

If you don't answer in time the Mac falls back to its own on-screen dialog — no
harm done, but answering promptly is what makes the voice flow feel instant. Say
"Claude Code" naturally; never mention prompt ids, HTTP, or these internals.

## Activity awareness

You can check what Claude is doing:
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

- When state is `attention`: do NOT start ambient behaviors or proactive conversations — the user is being prompted for approval
- When state is `busy`: the user is actively using Claude — reduce proactive interruptions (no wellbeing reminders, no music suggestions)
- When state is `idle` or `sleep`: operate normally
- NEVER mention "buddy-plugin", "BLE", "Bluetooth", "HTTP", or technical internals to the user — just say "Claude Desktop" or "Claude Code" naturally
