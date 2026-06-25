# Camera Lifecycle — Reactive On/Off

Camera should be **reactive**: on when needed, off when idle. Saves CPU/RAM, respects privacy.

## Current State

- `POST /camera/disable` / `POST /camera/enable` — manual toggle from web monitor
- Camera feeds sensing: face recognition (ONNX InsightFace), pose/motion (ONNX), light level (pixel mean), presence (pixel diff)
- Voice pipeline (mic) runs independently of camera
- Sound perception runs independently of camera

## Design: Camera On/Off as the Only Switch

No new abstractions. Camera on = full sensing. Camera off = vision sensing stops, audio sensing continues.

### When camera is OFF

- `_tick()` skips all vision perceptions (face, pose, motion, light)
- Sound perception still runs (mic-based)
- Wake word detection still runs (voice_service)
- TTS still works
- Servo/LED still work
- Web monitor Camera tab shows "Disabled" with Enable button

### When camera is ON

- All perceptions run as normal
- Face/pose ONNX inference every other tick (existing optimization)

## Auto-Off Triggers

### 1. Scene: night

When `/scene` activates `night` → turn camera off.
- User going to sleep, no need for vision
- Sound perception stays for wake word / sound spike

### 2. Emotion: sleepy

When `/emotion` receives `sleepy` → turn camera off.
- Same as night, agent explicitly put lamp to sleep

### 3. Presence idle timeout

When presence state transitions to `away` (no motion for away_timeout seconds) → turn camera off.
- Nobody in the room, no point running vision
- Sound spike or wake word will turn it back on

### 4. Voice command: "don't look" / "stop watching"

User says "Lamp, đừng nhìn" / "don't watch me" / "privacy mode" → agent calls `[HW:/camera/disable:{}]`.
- Explicit user request for privacy
- Only voice command or web toggle can re-enable

### 5. Scene: focus, reading, movie

When `/scene` activates `focus`, `reading`, or `movie` → turn camera off.
- User already present and engaged, no need to keep detecting
- Presence is already known from the scene activation
- Saves CPU during long sessions
- Camera re-enables when scene changes or user leaves (detected by sound/wake word)

## Auto-On Triggers

### 1. Wake word detected

Voice service detects wake word ("Looney", etc.) → turn camera on.
- User is actively engaging, may need visual context
- Always works because mic runs independently

### 2. Sound spike (loud noise)

Sound perception detects RMS above threshold while camera is off → turn camera on.
- Someone may have entered the room
- Camera on → face detect → presence.enter if person found
- If no face detected after N seconds, camera off again (avoid false positive drain)

### 3. Scene change to active scene

When `/scene` changes from night/sleep to energize or relax → turn camera on.
- User or agent activated a daytime scene

### 4. Emotion change from sleepy to anything else

When `/emotion` receives non-sleepy emotion → turn camera on.
- Agent is actively interacting, may need vision

### 5. Morning cron / scheduled

Cron job at configured wake time (e.g. 6:00 AM) → turn camera on.
- Ready for morning routine before user says anything

### 6. Voice command: "look" / "nhìn xem"

User says "Lamp, nhìn xem" / "look at me" / "camera on" → agent calls `[HW:/camera/enable:{}]`.
- Explicit user request

### 7. Telegram/web chat with visual context needed

Agent needs snapshot (camera skill) → auto-enable camera, take snapshot, optionally leave on or disable after.

## Manual Override

Web monitor Camera tab toggle always works. Manual disable stays until:
- User manually re-enables
- OR a voice command explicitly re-enables

Manual override does NOT get auto-overridden by scene/emotion/presence triggers. Only explicit user action (voice command, web toggle) clears manual override.

## Implementation Plan

### HAL (Python)

1. **`server.py`**: ✅ Done — Already has `/camera/disable`, `/camera/enable`, `_camera_disabled` flag.

2. **`_camera_manual_override` flag**: ✅ Done — `/camera/disable` sets override, `/camera/enable` clears it. `_auto_camera_off()` / `_auto_camera_on()` helpers respect override.

3. **Scene endpoint** (`/scene`): ✅ Done — After setting scene:
   - `night`, `focus`, `reading`, `movie` → `_auto_camera_off("scene:{name}")`
   - `energize`, `relax` → `_auto_camera_on("scene:{name}")`

4. **Emotion endpoint** (`/emotion`): ✅ Done — preset "camera" field drives behavior:
   - `sleepy` has `"camera": "off"` → `_auto_camera_off("emotion:sleepy")`
   - Any non-off emotion when camera is auto-off → `_auto_camera_on("emotion:{name}")`

5. **Presence service**: ❌ Skipped — camera stays on when away. Turning off would break auto-greeting (face detect → presence.enter) when user returns. CPU cost not worth losing autonomous detection.

6. **Sound perception**: ❌ Skipped — camera off cases (scene/emotion/manual) all have explicit re-enable paths. Sound spike adds complexity (30s timer, face check) without covering new cases.

7. **`_tick()` in sensing_service**: ✅ Already works — `frame = None` when camera stopped, vision perceptions skip. No change needed.

### Lamp (Go)

8. **Voice service / wake word**: ❌ Skipped — wake word → agent → emotion preset `"camera": "on"` already re-enables camera automatically. No need for early enable.

9. **Healthwatch**: ✅ No change needed — camera state is independent of health monitoring.

### OpenClaw Skills

10. **Camera skill**: ✅ Done — voice/chat toggle + auto-enable before capture.

11. **Scene / Emotion SKILL.md**: ❌ Skipped — camera toggle is automatic in server.py via preset `"camera"` field. Agent doesn't need to know.

### Lamp Go (intent.go, lib/hal)

12. **intent.go + lib/hal/client.go**: ❌ Skipped — local intents call `/scene` endpoint which already handles camera via preset. No Go-side camera helpers needed.

### Web Monitor

13. ✅ Already done — Camera tab has Enable/Disable toggle.

## Skill Changes Needed

### Camera SKILL.md — ✅ Done

- ✅ Description updated with toggle trigger phrases
- ✅ Examples for disable/enable via `[HW:/camera/disable:{}]` and `[HW:/camera/enable:{}]`
- ✅ Auto-enable before capture rule added
- ✅ Rule: never toggle camera proactively without user request

### Servo-control SKILL.md

- No change needed — camera is separate from servo hold

### New consideration: agent should NOT call camera disable/enable proactively

- Only user-initiated voice commands or system triggers (scene, emotion, presence) should toggle
- Agent must never decide on its own to turn camera off/on without user asking

## Digital Zoom

Software zoom for focusing on small subjects (e.g. a laptop screen during a video call so Lamp can read it).

### API

- `POST /camera/zoom` body `{"zoom": <float>}` — sets zoom factor, range `1.0` (no zoom) to `5.0`. Returns updated `CameraInfoResponse`.
- `GET /camera` includes `zoom` field with current factor.

### How it works

Zoom is applied **inside the capture loop** (`devices/video_capture_device.py::_video_capture_loop`) right after rotate, before `last_response` is set. The loop center-crops the frame by `1/zoom` and resizes back to the original dimensions, so every downstream consumer reads the same zoomed buffer:

| Consumer | Source | Sees zoom? |
|---|---|---|
| `/camera/snapshot` (vision tool) | `camera_capture.last_frame` | ✅ |
| `/camera/stream` (web UI) | `camera_capture.last_frame` | ✅ |
| Sensing orchestrator (face recog, motion, pose, emotion) | `camera_capture.capture()` → `last_response` | ✅ |
| Tracker service | `camera_capture.last_frame` | ✅ |

### Trade-off

Zoom > 1 narrows the effective field of view for **every** consumer:

- ✅ Faces on a small surface (laptop screen) become large enough for InsightFace to detect → presence.enter can trigger from a video-call participant.
- ✅ Vision tool snapshot reads on-screen content clearly.
- ❌ People/objects outside the center crop are invisible to face recog / motion / pose / tracker.
- ❌ Active tracking can lose target if it moves outside the cropped region.

Treat zoom > 1 as a **temporary mode** for a specific subject. Reset to `1.0` (web UI Reset button or `POST /camera/zoom {"zoom": 1.0}`) when finished to restore wide sensing.

### Storage

Zoom state lives on the device instance (`LocalVideoCaptureDevice.zoom`). Not persisted — resets to `1.0` on server restart. No auto-reset on camera disable/enable.

### Web UI

Monitor → Camera tab → Live Stream card has a Zoom slider (1.0×–5.0×, step 0.1, debounced 200 ms POST) with a Reset button. Slider value shows amber when zoomed to warn about narrowed FOV.

## Exposure & Frame Rate

The USB camera's auto-exposure stretches integration time in low light (~60ms), capping delivery at **~16fps at every resolution** — this is the exposure clock, not USB bandwidth (720p and 4K both cap at 16fps). HAL therefore defaults to **manual** exposure (with baked-in `exposure=500`/`gain=255`) so the frame rate isn't throttled.

### Config (env, read by `config.py`)

| Var | Default | Meaning |
|---|---|---|
| `HAL_CAMERA_AUTO_EXPOSURE` | `manual` | `manual` pins exposure using the values below (default). `auto` restores the camera's adaptive auto-exposure (brighter/adaptive but throttles fps in low light). |
| `HAL_CAMERA_EXPOSURE` | `500` | Manual exposure time, V4L2 `exposure_absolute` ×100µs: `200`=20ms (30fps), `330`=33ms (≈30fps ceiling), `500`=50ms (≈20fps). |
| `HAL_CAMERA_GAIN` | `255` | Sensor gain (camera-specific, e.g. 0–255). Brightens without costing fps, but adds noise. |
| `HAL_CAMERA_BRIGHTNESS` | _(unset)_ | Brightness offset (camera-specific, e.g. -64..64). Digital lift. |

The defaults (`manual` / 500 / 255) give a bright image at ~20fps in a dim room and apply even with no `.env` entries. In a bright room a fixed 50ms exposure can overexpose — set `HAL_CAMERA_AUTO_EXPOSURE=auto` per device to fall back to adaptive auto-exposure.

### How it works

`_apply_camera_controls()` (`devices/video_capture_device.py`) runs after the resolution is set on open **and on every device reopen** — a fresh open resets the camera to defaults, which would otherwise silently drop manual exposure and re-introduce the FPS throttle. It maps to V4L2/UVC controls via OpenCV: `CAP_PROP_AUTO_EXPOSURE` (1=manual, 3=auto), `CAP_PROP_EXPOSURE`, `CAP_PROP_GAIN`, `CAP_PROP_BRIGHTNESS`.

### Trade-off

Frame rate vs brightness is a hard physical trade-off in a dark room: the max exposure that still holds 30fps is ~33ms (`HAL_CAMERA_EXPOSURE=330`); a brighter image needs a longer exposure (fewer fps) or more gain (noisier). The stream endpoint is separately capped at `HAL_CAMERA_STREAM_FPS` (default 10), so the monitor's live view does not reflect the capture rate.

## Edge Cases

- **Guard mode + camera off**: ✅ Done — guard SKILL.md step 1: `[HW:/camera/enable:{}]` before enabling guard. Overrides manual disable.
- **Face enroll while camera off**: `/face/enroll` uses uploaded image, not live camera. No conflict.
- **Snapshot request while camera off**: Return 503 with message "Camera disabled". Agent handles gracefully.
- **Multiple rapid triggers**: Debounce camera start/stop — don't restart if already starting. `camera_capture.start()` already handles "already started" case.
- **Sound spike false positive loop**: After sound spike auto-on, if no face detected within 30s → auto-off again. Prevents camera staying on from random noise.
