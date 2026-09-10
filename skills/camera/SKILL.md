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

a photo was **already taken** for this exact request by the realtime voice layer (it captured the frame, then handed the turn to you — e.g. it timed out mid-answer), and the OS layer delivers it **with this very message** — either as an `[image description]` line (when the main model is text-only, a vision model has already analyzed the photo for you) or as an attached image. **Answer the visual question from that description/attachment. Do NOT call `/api/vision/look` or `/camera/snapshot` again** — re-snapshotting wastes time and may capture a different moment than what the user asked about. Do NOT read the path with a file tool — it is there for traceability only, and on text-only models a file-read image is silently dropped.

Use the capture protocol below when no current image or description was supplied for the request.

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
and image sizing. No preparatory aim or sleep is needed unless the user explicitly requested a movement (see Move first, then snapshot).

For raw-frame export rather than a visual answer, use
`GET http://127.0.0.1:5001/camera/snapshot?save=true&width=768&quality=75`
(HAL, port 5001). It writes a file and returns its path, without a description;
the path alone is not visual evidence.

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
the turn** (`POST /servo/aim`, `POST /servo/hold`), *then* call `/api/vision/look`. `[HW:...]`
markers are executed only after your reply is composed, so a marker-based aim
would move the device *after* the photo — you would describe the old view.

## Workflow
1. `POST http://127.0.0.1:5000/api/vision/look` with the user's question — **call it directly, never check /camera first**. It auto-enables the camera if disabled.
2. Respond from the returned `description`, or inspect the returned `path` with an image tool when the server provides only a path (see Capture Protocol).

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

**NEVER refuse a requested capture because camera is disabled.** `/api/vision/look` uses the HAL snapshot endpoint, which temporarily enables the camera, captures the frame, then restores the disabled state. Do NOT check `/camera` status or ask the user to enable it first. Use `/api/vision/look` for visual questions and the raw snapshot endpoint only for frame export.

## Error Handling
- If capture fails, report the returned error without describing an unseen frame. `/api/vision/look` reports capture/description failures as errors; a raw `/camera/snapshot` request can return 503 when the camera is unavailable.
- If the API is unreachable, inform the user that the camera is temporarily unavailable.
- **Never check `/camera` status before a visual request** — call `/api/vision/look` directly unless a current image/description was already supplied.
- If a sensing event already included an image, do not call the camera API again.

## Rules
- **Visual questions use `/api/vision/look`** — the server handles capture and model-compatible image evidence.
- **Raw-frame export uses `/camera/snapshot?save=true&width=768&quality=75`** — read the returned `path`; never invent filenames or treat a path as a description.
- **Image delivery is handled automatically by the system** — do not manually send images via tools.
- **Never use the camera proactively without the user's request** — respect privacy.
- **Never disable/enable camera on your own** — only toggle when the user explicitly asks or when a system trigger requires it (guard mode, scene change).
- **Don't repeatedly snapshot without reason.**
- **Don't call the camera API when a sensing event already included an image.**
- **Prefer `/api/vision/look` for one-time visual questions**; use raw snapshot for frame export and `/camera/stream` only for continuous video.
- When describing what you see, be specific and helpful.
- If camera is unavailable, inform the user clearly and move on.

## Output Template

After a visual tool call, answer from its description or the image you inspected. For a privacy toggle, include the corresponding marker:

```
[HW:/camera/disable:{}] Camera off.
```
