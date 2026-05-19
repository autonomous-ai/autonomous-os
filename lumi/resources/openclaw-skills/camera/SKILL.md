---
name: camera
description: Camera control — snapshot, stream, and privacy toggle. Trigger on "what do you see", "look at this", "take a photo", "don't look", "stop looking", "stop watching", "stop staring", "camera off", "camera on", "give me privacy". MUST call [HW:/camera/disable:{}] or [HW:/camera/enable:{}] when toggling — never just reply with text.
---

# Camera

## Quick Start
Accesses the lamp's built-in camera at `http://127.0.0.1:5001` to take snapshots or check the environment. Only use when the user explicitly asks you to look at something.

## Capture Protocol

Just call the snapshot endpoint — the server handles servo freeze, frame wait, and auto-enable if camera was disabled.

```bash
curl -s "http://127.0.0.1:5001/camera/snapshot?save=true&width=768&quality=75"
```

Returns JSON: `{"path": "/root/.openclaw/media/lumi-snapshots/snap_1712567890123.jpg"}`.
**Never hardcode a filename** — always read `path` from the response.

`width=768&quality=75` shrinks the JPEG (~50–80 KB instead of ~300–500 KB at full 1920×1080) so vision LLM uploads + tokenizes faster. 768 px wide is still enough to read text on a laptop screen and recognize people/objects. Do NOT remove these unless you specifically need a larger image.

No need to aim servo or sleep before snapshot — the server freezes servos automatically for a stable frame.

## Workflow
1. Call `GET /camera/snapshot?save=true&width=768&quality=75` — **always call directly, never check /camera first**. The endpoint auto-enables camera if disabled.
2. Analyze the image and describe what you see.
3. Respond helpfully and specifically to the user's question.

You also receive camera snapshots **automatically** as part of sensing events (`[sensing:*]` messages with images). You do not need the camera API for those — just look at the attached image.

## Examples

**Input:** "What do you see right now?"
**Output:** `GET /camera/snapshot?save=true&width=768&quality=75` → analyze image. Say: "I can see your desk with a laptop and a coffee mug. Looks like a productive setup!"

**Input:** "Is anyone in the room?"
**Output:** `GET /camera/snapshot?save=true&width=768&quality=75` → analyze image. Say: "I can see one person sitting at the desk."

**Input:** "Take a photo" or "Send me a photo"
**Output:** `GET /camera/snapshot?save=true&width=768&quality=75` → read `path` from JSON → describe what you see.

**Input:** (sensing event with image already attached)
**Output:** Do NOT call the camera API. Just look at the attached image and react.

## Tools

**Bash** with `curl` for HTTP calls to `http://127.0.0.1:5001`.

### Take a snapshot

```bash
curl -s "http://127.0.0.1:5001/camera/snapshot?save=true&width=768&quality=75"
```

Returns JSON with the saved file path:
```json
{"path": "/root/.openclaw/media/lumi-snapshots/snap_1712567890123.jpg"}
```

Without `?save=true`, returns raw JPEG bytes (used by web UI).

### Live stream

```bash
curl -s http://127.0.0.1:5001/camera/stream
```

Returns an MJPEG stream (`multipart/x-mixed-replace`). Only use when continuous video is needed. Prefer snapshot for one-time checks.

## Camera On/Off (Privacy Control)

Users can toggle the camera via voice or chat. Use HW markers — no curl needed.

### Disable camera

```
[HW:/camera/disable:{}]
```

The user wants privacy. Camera stays off until the user explicitly re-enables it (voice or web toggle).

### Enable camera

```
[HW:/camera/enable:{}]
```

### Trigger phrases (MANDATORY — must call HW marker, not just reply with text)

Any phrase meaning "stop looking" or "camera off" MUST trigger `[HW:/camera/disable:{}]`. Any phrase meaning "look at me" or "camera on" MUST trigger `[HW:/camera/enable:{}]`. Do NOT just acknowledge — you MUST include the HW marker.

| User says | Action |
|-----------|--------|
| "don't look" / "stop looking" / "stop watching" / "privacy mode" / "camera off" / "don't watch me" / "give me privacy" / "stop staring" | `[HW:/camera/disable:{}]` — MUST call |
| "look at me" / "camera on" / "you can look now" / "start watching" / "look at this" | `[HW:/camera/enable:{}]` — MUST call |

### Examples

**Input:** "Lumi, don't watch me"
**Output:** `[HW:/camera/disable:{}]` Got it, camera off. Just say "look at me" when you want me to see again.

**Input:** "Stop watching me"
**Output:** `[HW:/camera/disable:{}]` I'll look away. Let me know when you want me back.

**Input:** "Lumi, look at me"
**Output:** `[HW:/camera/enable:{}]` Camera back on!

### Auto-enable on snapshot (IMPORTANT)

**NEVER refuse a snapshot because camera is disabled.** The `/camera/snapshot` endpoint auto-enables the camera, captures the frame, then re-disables it automatically. Do NOT check `/camera` status before snapshot. Do NOT ask the user to enable camera first. Just call the endpoint.

## Error Handling
- If `/camera/snapshot` returns 503, tell the user: "The camera is not connected right now."
- If the API is unreachable, inform the user that the camera is temporarily unavailable.
- **Never check `/camera` status before snapshot** — just call `/camera/snapshot` directly.
- If a sensing event already included an image, do not call the camera API again.

## Rules
- **Just call `/camera/snapshot?save=true&width=768&quality=75`** — server handles servo freeze and camera enable automatically.
- **Always use `?save=true`** and read the `path` from the JSON response — never invent filenames.
- **Image delivery is handled automatically by the system** — do not manually send images via tools.
- **Never use the camera proactively without the user's request** — respect privacy.
- **Never disable/enable camera on your own** — only toggle when the user explicitly asks or when a system trigger requires it (guard mode, scene change).
- **Don't repeatedly snapshot without reason.**
- **Don't call the camera API when a sensing event already included an image.**
- **Prefer `/camera/snapshot`** over `/camera/stream` — simpler and sufficient for most tasks.
- When describing what you see, be specific and helpful.
- If camera is unavailable, inform the user clearly and move on.

## Output Template

```
[Camera] Action: {snapshot|stream|check}
Available: {yes|no}
Description: {what you see in the image}
```
