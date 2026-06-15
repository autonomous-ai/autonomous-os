# Vision Tracking — Object Follow with Servo

Lamp can track and follow any object the user names. Two-stage approach: YOLOWorld API detects the object by name, TrackerVit follows it in real-time.

## Architecture

```
User: "Lamp, follow the cup"
         |
    POST /servo/track {"target": "cup"}
         |
    1. YOLOWorld API: frame + "cup" → bbox [x,y,w,h]  (~1-2s, RunPod GPU)
         |
    2. TrackerVit init on bbox
         |
    3. Tracking loop @ 7 FPS (move-then-freeze cadence)
         |  grab frame (servo stationary) → TrackerVit update → nudge → wait for servo to settle
         |
    4. Object moves → servo follows (yaw + 3 pitch joints)
         |
    5. Confidence < 0.3 for 5 frames → auto-stop + hold servo at current pose
```

### Why move-then-freeze (not high-FPS chasing)

Earlier iterations ran the loop at 20 FPS, commanding servo nudges every 50ms. Two problems:

1. **Camera ego-motion blur.** The camera is mounted on the moving lamp head. Commanding the servo faster than it can physically execute means frames are captured mid-motion — blurred or offset from what the tracker "sees". The tracker then computes bbox from a frame that no longer represents the current servo pose, and the nudge overshoots.
2. **Command stacking.** Small nudges (~0.5°) every 50ms stacked up faster than the motor could reach targets, producing visible hunting and twitching.

The current design reads a frame, decides one nudge, sends it, then explicitly waits for the servo to physically complete the move (~80ms) before reading the next frame. Each frame is sharp and coordinates match the current pose. Fewer commands, bigger deliberate steps, no hunting.

### Why there is no periodic YOLO re-detect

Earlier versions called YOLOWorld every 5 seconds during active tracking to correct drift. This was removed because the YOLO round-trip is 1-2 seconds, during which:

- The servo continues moving — the returned bbox is in coordinates that no longer match the current frame.
- The object itself may have moved.
- The scene can change arbitrarily.

Using that bbox to re-init the tracker caused more harm than good. Drift is now handled by the TrackerVit confidence score: if it drops below threshold for 5 frames, tracking stops cleanly and the caller can re-issue the follow command.

### Detection: YOLOWorld API

Open-vocabulary object detection — detects any object by text label, not limited to fixed classes.

- **Endpoint:** `{DL_BACKEND_URL}/detect/yoloworld`
- **Auth:** `x-api-key` header from `DL_API_KEY` config
- **Request:** `{"image_b64": "...", "classes": ["cup"]}`
- **Response:** `[{"class_name": "cup", "xywh": [cx, cy, w, h], "confidence": 0.98}]`
- **Speed:** ~1-2s (RunPod GPU)

Used automatically when `POST /servo/track` is called without `bbox`. Can also provide bbox manually to skip detection.

### Tracking: TrackerVit

Real-time object following after initial detection.

## Tracker: TrackerVit

**Model:** `os/hal/drivers/tracking/models/vittrack.onnx` (714KB, checked into repo)

| Feature | Value |
|---------|-------|
| Speed | ~10-20ms/frame on Pi 5 |
| Confidence score | 0.0-1.0 per frame |
| Scale handling | Auto-adjusts bbox size |
| Loss detection | Returns `ok=False` + low score when object disappears |

**Fallback chain:** TrackerVit → CSRT (needs opencv-contrib) → KCF → MIL

## Servo Control

Tracking uses all 4 pitch/yaw servos:
- **base_yaw** (ID 1) — left/right pan (100% of yaw)
- **base_pitch** (ID 2) — up/down tilt, 55% of pitch
- **elbow_pitch** (ID 3) — up/down tilt, 30% of pitch
- **wrist_pitch** (ID 5) — up/down tilt, 15% of pitch

Pitch is distributed across the 3 arm joints (base 0.55 / elbow 0.30 / wrist 0.15). Primary tilt on base, secondary on elbow, minimal on wrist — reduces mechanical interference and makes the lamp head *lead* the motion instead of three joints twitching together.

**During tracking:**
- `_hold_mode = True` — suppresses idle animation so the tracker owns the servos.
- EMA smoothing on bbox center (`EMA_ALPHA = 0.3`) — filters TrackerVit jitter before converting to degrees.
- Bbox-jump fallback (`BBOX_JUMP_PX = 120`) — if the tracker reports the center moved more than 120px in one frame, treat it as a partial glitch and fall back to EMA-smoothed center rather than dropping the frame.
- Periodic YOLO re-detect (`REDETECT_INTERVAL_S = 5.0`) — call YOLOWorld every 5s to correct tracker drift by re-seeding the bbox.
- Bus position re-read each cycle — resync internal pose from hardware so external motion (scene change, stale animation, manual command) doesn't compound stale deltas.

### Pixel-to-Degree Conversion

```
Frame center: (320, 240) for 640x480
Object center: EMA-smoothed tracker bbox center (alpha = 0.3)

dx = cx - 320   (positive = right)
dy = cy - 240   (positive = below)

yaw_deg   = dx * 0.022   (clamped to ±4.5°, zero if |dx| < 12)
pitch_deg = dy * 0.022   (clamped to ±4.5°, zero if |dy| < 12)

Adaptive gain: when |dx| or |dy| > 120px, multiply gain by 1.3x
to catch up faster without overshoot. Stays at 1.0 when closer.
```

### Tuning Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `DEG_PER_PX_YAW` | 0.022 | Degrees per pixel horizontal |
| `DEG_PER_PX_PITCH` | 0.022 | Degrees per pixel vertical |
| `DEAD_ZONE_PX` | 12 | Ignore offsets smaller than this (anti-jitter) |
| `WAKE_ZONE_PX` | 40 | When settled, only resume nudging when object moves beyond this |
| `ADAPTIVE_GAIN_PX` | 120 | Above this offset, boost gain to catch up |
| `ADAPTIVE_GAIN_MULT` | 1.3 | Gain multiplier when object is far from center |
| `MAX_NUDGE_DEG` | 4.5 | Max degrees per step (tuned for TRACK_FPS=20) |
| `TRACK_FPS` | 20 | Tracking loop frequency (~50ms/cycle) |
| `EMA_ALPHA` | 0.3 | Bbox center smoothing factor |
| `BBOX_JUMP_PX` | 120 | Tracker jump threshold — fallback to EMA-smoothed center |
| `REDETECT_INTERVAL_S` | 5.0 | Periodic YOLO re-detect to correct drift |
| `CONFIDENCE_THRESHOLD` | 0.3 | Below this = "lost" |
| `MAX_LOW_CONFIDENCE_FRAMES` | 5 | Consecutive low-confidence frames before auto-stop |
| `PITCH_WEIGHT_BASE/ELBOW/WRIST` | 0.55 / 0.30 / 0.15 | Pitch distributed across 3 joints |

### Servo Position Limits

| Joint | Min | Max |
|-------|-----|-----|
| base_yaw | -135 | 135 |
| base_pitch | -90 | 30 |
| elbow_pitch | -90 | 90 |
| wrist_pitch | -90 | 90 |

## Auto-Stop Conditions

TrackerVit provides confidence scoring, unlike MIL/KCF which silently drift. Tracking auto-stops and holds the servo at its last position in these cases:

| Condition | Action |
|-----------|--------|
| `confidence < 0.3` for 5 frames | Stop — lost target |
| Bbox area > 3x initial size | Stop — tracker drift/bloat |
| Bbox covers > 50% of frame | Stop — tracker drift |
| Servo at yaw/pitch limit + object still >30% off center | Stop — object unreachable |
| Tracking duration > 5 minutes | Stop — timeout to save motor/CPU |
| `tracker.update()` returns `ok=False` | Count as low-confidence frame |

### Auto-stop on gateway/network disconnect

Object tracking is driven by remote vision updates from the agent/cloud. When the gateway WebSocket disconnects (cloud or internet loss), the device auto-stops any in-flight servo tracking — `os/services/internal/openclaw/service_ws.go` calls `hal.StopServoTracking()` → HAL `POST /servo/track/stop` (best-effort, guarded by `SetUpCompleted`). Without fresh remote updates, continued tracking would keep aiming the body at a stale target it can no longer correct, so it is stopped as a safety reflex. Local idle animation continues (the device stays "alive", doesn't freeze) and recovery (`/servo/track/stop`, stop/release) stays available. See `devices/lamp/SAFETY.md` → `## fail-safe states` (Network/gateway loss row, enforced).

## API Endpoints

All under `/servo/track`.

### GET /servo/track/targets — List suggested targets

```json
{"targets": ["person", "cup", "bottle", "glass", "phone", "laptop", ...]}
```

YOLOWorld is open-vocabulary — any text works, this list is just suggestions.

### POST /servo/track — Start tracking

`target` accepts either a single string or a list of candidate labels. When a list is passed, YOLOWorld evaluates all labels and the single highest-confidence detection is used. Useful when the caller (e.g. an LLM skill) is unsure which exact label will match.

```json
// Auto-detect, single label
{"target": "cup"}

// Auto-detect, list of candidate labels (preferred from LLM skills)
{"target": ["cup", "mug", "coffee cup"]}

// Manual bbox (skip detection — target is for display only)
{"bbox": [190, 50, 170, 300], "target": "cup"}

// Response
{
  "status": "ok",
  "tracking": true,
  "target": "cup | mug | coffee cup",
  "bbox": [190, 50, 170, 300],
  "confidence": 1.0
}
```

### POST /servo/track/stop — Stop tracking

```json
{"status": "ok", "tracking": false}
```

### GET /servo/track — Check status

```json
{
  "status": "ok",
  "tracking": true,
  "target": "cup",
  "bbox": [195, 55, 175, 295],
  "confidence": 0.612
}
```

### POST /servo/track/update — Re-initialize bbox

Manual re-init of the tracker with a new bbox without stopping the session.

```json
{"bbox": [250, 160, 75, 95], "target": "cup"}
```

Note: there is no automatic periodic YOLO re-detect — the caller decides when to re-init. See "Why there is no periodic YOLO re-detect" above.

## End-to-End Flow

### Happy path

```
1. User: "Lamp, follow the cup"
2. Agent calls POST /servo/track {"target": "cup"}
3. HAL internally:
   a. Snapshots a frame and holds on to it
   b. Sends that frame to YOLOWorld API → gets bbox (~1-2s)
   c. TrackerVit init uses the *same* frame + bbox (coordinates match)
   d. Starts the move-then-freeze tracking loop
4. Servo follows the cup in real-time (confidence ~0.5-0.7)
5. User: "OK stop" → agent calls POST /servo/track/stop
6. Servo holds at current position (no snap-back to idle)
```

### Auto-stop on lost

```
1. Object leaves frame or is occluded
2. TrackerVit confidence drops below 0.3
3. After 5 consecutive low-confidence frames → auto-stop
4. Servo holds at last known position (no snap-back)
5. Agent can notify user or auto re-detect
```

## Camera Stream Overlay

When tracking is active, the MJPEG stream (`/camera/stream`) draws:
- Green bounding box around tracked object
- Target label above the box

## Web UI

Camera section shows:
- **Vision Tracking card** — target input, bbox input, Start/Stop/Status buttons
- **Stream badge** — "LIVE" or "TRACKING: {target}"
- **Confidence** — shown in tracking info panel
- **Polling** — status refreshes every 3 seconds

## Dependencies

- `opencv-python>=4.8.0` (already in `pyproject.toml`)
- `vittrack.onnx` — checked into repo at `os/hal/drivers/tracking/models/vittrack.onnx`
- `requests` (already in project)
- **YOLOWorld API** — RunPod DL backend at `DL_BACKEND_URL/detect/yoloworld`

## Interaction with Other Systems

| System | During tracking | After tracking |
|--------|----------------|----------------|
| Servo idle animation | Suppressed (`_hold_mode`) | Resumed |
| `/servo/play` | Blocked by `_hold_mode` | Resumed |
| Sensing (face, motion) | Continues — shares camera | Continues |
| Camera stream overlay | Green bbox drawn | Normal stream |
| TTS | Continues normally | Continues normally |

## Next Steps

- **OpenClaw skill** — `track/SKILL.md` so agent can call tracking via voice
- ~~**Periodic re-detect**~~ — tried, rolled back. 1-2s YOLO round-trip desyncs from servo motion (see "Why there is no periodic YOLO re-detect" above)
- **PID control** — smoother servo response instead of proportional-only
- **Multi-object** — track multiple objects, switch between them
