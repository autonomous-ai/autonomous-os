---
description: Push current Claude Code usage to your device
allowed-tools: Bash(*)
---

Fetch real-time Claude Code usage and push it to your device.
Follow SKILL.md (Usage) to:

1. Get OAuth token from Keychain / credentials file
2. Fetch usage from the API (5-hour and 7-day windows)
3. Build the `/claude-code/usage` payload (`five_hour`, `seven_day`, `reset_5h`, `reset_7d`, `sound`)
4. POST it to the device's `:5002` `/claude-code/usage` endpoint
5. Report the percentages and reset times
