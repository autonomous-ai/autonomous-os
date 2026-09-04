---
name: camera
description: Camera control — snapshot, stream, and privacy toggle. Trigger on "what do you see", "look at this", "take a photo", "don't look", "stop looking", "stop watching", "stop staring", "camera off", "camera on", "give me privacy". MUST call [HW:/camera/disable:{}] or [HW:/camera/enable:{}] when toggling — never just reply with text.
---

# Camera

## Quick Start
Accesses the device's built-in camera at `http://127.0.0.1:5001` to take snapshots or check the environment. Only use when the user explicitly asks you to look at something.

## Already-captured frame (reuse, don't re-snapshot)

If the incoming turn contains a line like:

```
[vision-image] <absolute-path-to-a.jpg> (a photo was JUST captured ...)
```

a photo was **already taken** for this exact request by the realtime voice layer (it captured the frame, then handed the turn to you — e.g. it timed out mid-answer), and the OS layer delivers it **with this very message** — either as an `[image description]` line (when the main model is text-only, a vision model has already analyzed the photo for you) or as an attached image. **Answer the visual question from that description/attachment. Do NOT call `/camera/snapshot`** — re-snapshotting wastes time and may capture a different moment than what the user asked about. Do NOT read the path with a file tool — it is there for traceability only, and on text-only models a file-read image is silently dropped.

Only fall back to the snapshot endpoint below when there is no `[vision-image]` line.

## Capture Protocol

One call. It takes the photo, sizes it, and gives you back what is in it:

```bash
curl -sX POST http://127.0.0.1:5000/api/vision/look \
  -H 'Content-Type: application/json' \
  -d '{"question":"<what the user asked>"}'
```

Returns `{"data":{"description":"...","path":"..."}}` — **answer from
`description`**. It is the only thing in this turn that actually saw the frame.

- No `description` field (the main model can read images itself) → open `path`
  with your file/image tool.
- Any error → tell the user you couldn't see it this time. **Do not guess.**

The server handles servo freeze, frame wait, auto-enable if the camera was off,
and image sizing. Do not aim or sleep before calling it.

`GET http://127.0.0.1:5001/camera/snapshot` (HAL, port 5001) only writes a file
and returns its path — you cannot see that file. Use it only when you need the
raw frame for something other than looking at it.

## Never describe the view without an image

If you are about to say what you see, this turn MUST contain either a
`[vision-image]` line or a `/api/vision/look` call whose answer you actually
looked at, or a `/api/vision/look` description. Describing the room from memory, from an earlier turn's photo, or
from a plausible guess ("same view — the desk, your screen…") is a fabrication,
even when the guess happens to be close. No image → say you'll take a look and
take one; never invent.

## Move first, then snapshot

When the request combines a movement and a visual question ("turn right, hold
it there, and tell me what you see"), fire the servo calls **with curl during
the turn** (`POST /servo/aim`, `POST /servo/hold`), *then* look. `[HW:...]`
markers are executed only after your reply is composed, so a marker-based aim
would move the device *after* the photo — you would describe the old view.

## Workflow
1. `POST http://127.0.0.1:5000/api/vision/look` with the user's question — **call it directly, never check /camera first**. It auto-enables the camera if disabled.
2. Respond helpfully and specifically to the user's question, from the `description` it returns.

You also receive camera snapshots **automatically** as part of sensing events (`[sensing:*]` messages with images). You do not need the camera API for those — just look at the attached image.

## Examples

**Input:** "What do you see right now?"
**Output:** `POST /api/vision/look` → say: "I can see your desk with a laptop and a coffee mug. Looks like a productive setup!"

**Input:** "Is anyone in the room?"
**Output:** `POST /api/vision/look` → say: "I can see one person sitting at the desk."

**Input:** "Take a photo" or "Send me a photo"
**Output:** `POST /api/vision/look` → say what `description` reports.

**Input:** (sensing event with image already attached)
**Output:** Do NOT call the camera API. Just look at the attached image and react.

## Tools

**Bash** with `curl` — `http://127.0.0.1:5000` for `/api/vision/look`, `http://127.0.0.1:5001` for HAL camera control.

### Look at the scene

```bash
curl -sX POST http://127.0.0.1:5000/api/vision/look -H 'Content-Type: application/json' -d '{"question":"..."}'
```

Returns `{"data":{"description":"...","path":"..."}}`. See *Capture Protocol*.

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
| "look at me" / "camera on" / "you can look now" / "start watching" | `[HW:/camera/enable:{}]` — MUST call |

### "Look at ..." is ambiguous — route by what follows

The verb alone does NOT mean "turn the camera on". Only phrases about the *device's own
camera state* belong in the table above.

| User says | Meaning | Route to |
|-----------|---------|----------|
| "look at me" / "camera on" / "you can look now" | turn the camera back on | `[HW:/camera/enable:{}]` |
| **"look at this"** / "look at what I'm holding" / "what is this" | a visual question about an object | **`/api/vision/look`** (Workflow above) |
| "look at the desk / table / wall" | a fixed location | `servo-control` `/servo/aim` |
| "look at the cup and follow it" | a movable object to track | `servo-tracking` `/servo/track` |

**"Look at this" is a visual question, not a privacy toggle.** The user is holding something
up to be identified. Replying "Got it, camera on" answers a question they did not ask.

### Examples

**Input:** "Look at this" / "Look at what I'm holding"
**Output:** `POST /api/vision/look` → say what the object is. Do NOT call `[HW:/camera/enable:{}]` — the look endpoint auto-enables the camera.

**Input:** "Don't watch me"
**Output:** `[HW:/camera/disable:{}]` Got it, camera off. Just say "look at me" when you want me to see again.

**Input:** "Stop watching me"
**Output:** `[HW:/camera/disable:{}]` I'll look away. Let me know when you want me back.

**Input:** "Look at me"
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
