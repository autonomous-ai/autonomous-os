---
description: Send a notification to your device
allowed-tools: Bash(*)
---

Send a notification to your device.
Follow SKILL.md (Notify) to build and POST the `/claude-code/notify` payload
(`title`, `subtitle`, `level`, `sound`) to the device's `:5002` `/claude-code/notify` endpoint.

If the user provided a message, use it as the notification title.
If no message, ask what to send.
