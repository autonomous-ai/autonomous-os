---
name: emotion-acknowledge
description: "Triggers a 'thinking' emotion on Lamp immediately when a message is received, before the LLM starts processing"
homepage: https://github.com/autonomous-ai/lamp
metadata:
  { "openclaw": {
      "emoji": "🤔",
      "events": ["message:preprocessed"],
      "requires": { "bins": ["node"] }
    }
  }
---

# emotion-acknowledge

Fires at `message:preprocessed` — before the LLM turn begins — and calls the LeLamp emotion API to show a `thinking` state on the lamp hardware (servo + LED + display eyes).

This bridges the silence gap between message arrival and LLM first response.

## Behavior

- Skips `[sensing:*]` messages — sensing events have their own defined emotion reactions
- Skips empty messages
- Calls `POST http://127.0.0.1:5001/emotion` with `{"emotion": "thinking", "intensity": 0.7}`
- Fails silently — never blocks message delivery
