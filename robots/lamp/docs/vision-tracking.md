# Vision Tracking — Object Follow with Servo

Lamp can track and follow any object the user names. A detector finds the object by name and seeds a ViT tracker, then a fast vision loop follows it in real time while a decoupled servo worker glides the head smoothly toward the target.

All tracking code lives in the `hal/drivers/tracking/` package:

| Module | Contents |
|--------|----------|
| `tracker_service.py` | `TrackerService` — session lifecycle (start/stop/status/update_bbox) + the fast vision loop |
| `constants.py` | All tuning knobs (imported as `C` by the other modules) |
| `detection.py` | `ObjectDetector` — YuNet face / local YOLOv8n / remote YOLOWorld chain |
| `vit_tracker.py` | OpenCV tracker backend (`create_tracker`, `vit_init`, `vit_update`, `get_tracking_score`) |
| `servo_follow.py` | `ServoFollower` — servo goal + SmoothDamp follow worker thread |
| `filters.py` | `AlphaBetaFilter2D`, `PID`, `smooth_damp`, `soft_deadband` |
| `frame_utils.py` | `downscale`, `scale_bbox` (coordinate mapping) |

## Architecture

```
User: "Lamp, follow the cup"
         |
    POST /servo/track {"target": "cup"}
         |
    1. Freeze servos 0.3s → grab a sharp frame
         |
    2. Detect the object (YuNet face | local YOLOv8n | remote YOLOWorld) → bbox
         |
    3. TrackerVit init on the bbox
         |
    4. Two decoupled threads:
         |   a. Vision loop @ FAST_LOOP_FPS (15):
         |        ViT update → alpha-beta centroid filter → soft dead zone
         |        → PID + velocity feedforward → publish an absolute servo goal
         |        (background YOLO re-detect every 1.5s corrects drift)
         |   b. Servo worker: SmoothDamp glide toward the latest goal
         |        (ease-in/ease-out; coalesces tiny setpoint changes)
         |
    5. Lost / bloated / no-detect / timeout → auto-stop, hold or return to zero
```

The vision loop never blocks on motor motion: it publishes an *absolute* servo goal and moves on to the next frame. The servo worker owns the physical motion and continuously eases toward whatever the latest goal is. This is what keeps both the tracker fps high and the head motion smooth.

### Downscaled vision, original-resolution math

The camera runs **1280×720**. Every heavy vision component — the ViT tracker and all three detectors — runs on a frame downscaled to `VISION_MAX_WIDTH` (640 px wide, 0.5× → ¼ the pixels) for speed. Each bbox they produce is mapped **back to original 1280×720 coordinates** before any servo/PID math (`frame_utils.downscale` / `scale_bbox`, `vit_tracker.vit_init` / `vit_update`, and `detect_object` is transparent). Because the coordinate contract downstream is always original resolution, none of the pixel-tuned constants (PID gains, gates, dead zones, feedforward thresholds) change when the downscale factor changes. Set `VISION_MAX_WIDTH = 0` to disable.

## Detection

`detect_object(frame, target)` returns a bbox `(x, y, w, h)` in original camera coords, trying three paths in order:

| Path | Detector | When | Speed (A523) |
|------|----------|------|--------------|
| 0 | **YuNet** face detector (`face_detection_yunet_2023mar.onnx`) | target ∈ {`face`, `human face`, `khuôn mặt`, `mặt`} | ~30 ms |
| 1 | **Local YOLOv8n** (COCO classes, `yolov8n.pt`, imgsz=320) | target maps to a COCO class | ~260–770 ms |
| 2 | **Remote YOLOWorld** open-vocab (`{DL_BACKEND_URL}/detect/yoloworld`) | non-COCO target, or local miss (fallback) | ~1.3–2.8 s |

- COCO has no hand/face class, so `hand`/`face` intentionally fall through to YuNet/YOLOWorld instead of mapping to `person` (which locked onto the whole body).
- On a local-YOLO miss the code falls back to remote YOLOWorld, **throttled** to at most one attempt per `REMOTE_FALLBACK_MIN_INTERVAL` (2.0 s) so a genuinely unseeable target doesn't hit remote every redetect.
- Detection quality filters: confidence ≥ `DETECT_MIN_CONFIDENCE` (0.15), area between `DETECT_MIN_AREA_RATIO` (0.3%) and `DETECT_MAX_AREA_RATIO` (80%) of frame.
- **Lookalike guard (local path)** — local YOLO detects **unrestricted** (no `classes=` filter) so competing classes stay visible, then: (a) the confusion cluster cell phone / mouse / remote needs conf ≥ 0.35 (`_CONFUSABLE_CONF_FLOOR`) instead of the global 0.15; (b) **cross-class disambiguation** — if a box of another class overlaps the candidate (IoU ≥ 0.5) with *higher* confidence, the candidate is rejected ("that's probably a mouse, not the phone you asked for") and the code falls through to the remote fallback. The 0.35 floor applies only to the session-start detect (`strict=True`); mid-session redetects use the global 0.15 floor (a fast-moving phone reconfirms at conf 0.2–0.3, and the reinit gates already protect the lock) — cross-class disambiguation stays on in both modes.

Weights are checked into the repo (`hal/drivers/tracking/models/`) so deploy is one rsync and the Pi never needs internet at boot to start tracking.

## Tracker: TrackerVit

**Model:** `hal/drivers/tracking/models/vittrack.onnx` (checked into repo)

| Feature | Value |
|---------|-------|
| Speed | ~15–25 ms/frame on the downscaled frame |
| Confidence score | `getTrackingScore()` 0.0–1.0 per frame |
| Scale handling | Auto-adjusts bbox size |
| Loss detection | Returns `ok=False` + low score when object disappears |

**Fallback chain:** TrackerVit → CSRT → KCF → MIL. Only ViT exposes a confidence score (used for ghost-lock detection); the others return 1.0.

## Servo Control

Tracking drives 4 joints:

- **base_yaw** (ID 1) — left/right pan (100 % of yaw)
- **base_pitch** (ID 2) — up/down tilt, 10 % of pitch
- **elbow_pitch** (ID 3) — up/down tilt, 90 % of pitch
- **wrist_pitch** (ID 5) — up/down tilt, 0 %

Pitch is concentrated on the elbow (`PITCH_WEIGHT_ELBOW = 0.90`). Empirically only pure-rotation joints move the object toward center; base/wrist mostly translate the camera (kinematic coupling), so their weights are low/zero. The elbow motor's positive direction was reversed in hardware, so its contribution carries `ELBOW_PITCH_SIGN = -1.0`.

### Control law (vision loop → servo goal)

Each frame the loop turns the tracker bbox into an absolute servo goal:

1. **Alpha-beta filter on the centroid** (`AlphaBetaFilter2D`) — a constant-velocity steady-state Kalman. Smooths jitter, coasts through dropped/garbage frames on prediction, gates outlier teleports (`AB_GATE_PX`), and exposes a velocity estimate. A velocity lead (`AB_LEAD_S = 0.20 s`) aims ahead of the target — cinematic "lead room".
2. **Tiered dead zone** (`soft_deadband`) — three bands, continuous at both boundaries: true zero inside ±`DEAD_ZONE_INNER_PCT` (2 %, PID clears and, when there is no velocity-pursuit command, the follower is retargeted to its current pose so it cannot continue toward a stale goal); a gentle **creep band** up to the outer edge (`DEAD_ZONE_CREEP_GAIN` = 0.12 slope) so the camera lazily drifts toward center instead of freezing dead — a hard stop here produced the start-stop "security camera" feel; full error beyond the outer edge.
3. **Velocity feedforward first, PID second (smooth pursuit)** — the primary command is a feedforward term proportional to the target's measured pixel velocity (`VFF_GAIN` = 0.9): the camera *matches the target's speed* like human smooth pursuit, even at zero position error. A time-aware PID with anti-windup (KP deliberately small: 0.015 yaw / 0.02 pitch) only trims the residual position error. A position-centered but moving target keeps panning (does not freeze in the dead zone). Combined output is clamped to `PID_OUTPUT_MAX_DEG` (5°).
4. **Saccade vs pursuit profiles** — mirroring human gaze: offset > `SACCADE_OFFSET_FRAC` (22 % of frame width) switches the follow worker to the **saccade** profile (`SACCADE_SMOOTH_TIME` 0.20 s, `SACCADE_MAX_SPEED_DPS` 100) for a fast relocation; small offsets use the heavy **pursuit** profile (`SERVO_SMOOTH_TIME` 0.32 s, `SERVO_MAX_SPEED_DPS` 55) — fluid-head inertia. One compromise profile did both badly. The loop state logs `SACCADE` vs `CHASING`.
5. **Publish goal** — the resulting absolute joint target is handed to the servo worker (non-blocking).

### Servo worker (SmoothDamp follower)

`ServoFollower` (`servo_follow.py`) runs a worker on its own thread and continuously eases the joints toward the latest goal using **SmoothDamp** (`smooth_damp`, a critically-damped follower): each joint carries its own velocity, so every move accelerates smoothly and eases out into the target, and a fresh goal arriving mid-move retargets without a restart jerk — the cinematic "film camera" motion. The worker wakes at the bounded `SERVO_SUBSTEP_SLEEP` (30 ms) cadence but calculates SmoothDamp from the actual elapsed monotonic time, capped at `SERVO_SUBSTEP_MAX_DT_S` (60 ms) after a scheduler/serial stall. It sends one multi-joint bus command only when at least one servo command changes by `SERVO_COMMAND_MIN_DELTA` (0.08), coalescing only tiny normalized setpoint changes; the final target is always sent once.

Hardware motion during tracking: at every session start the HAL explicitly writes `TRACKING_GOAL_VELOCITY = 0` (unlimited) to clear any velocity cap left by an earlier mode. The software profiles therefore own the speed; the old 150 steps/s ≈ 13°/s cap flattened the SmoothDamp curves into a constant crawl. `TRACKING_ACCELERATION = 30` supplies the gentle hw ramp. The return-to-zero glide is capped at `TRACKING_RETURN_VELOCITY` (200 steps/s); snappy defaults restored after.

### Drift correction & lock management

- **Background YOLO re-detect** every `YOLO_REDETECT_S` (1.5 s) on a worker thread (never blocks the fast loop; result delivered via a `maxsize=1` queue). Forced immediately when the object nears a frame edge (>25 %) or on the first tracker miss.
- **Miss-coast** — when the tracker misses while the target was moving (fast wave, motion blur), the loop keeps panning along the target's last alpha-beta velocity for up to `MISS_COAST_FRAMES` (6) miss frames (state `COAST`) before falling back to the search sweep. Stopping dead on the first miss guaranteed a fast target exited the frame before the redetect landed.
- **Reinit gating (SORT/ByteTrack-style)** — a re-detect only reinitializes the tracker when it has clearly diverged, to avoid the reinit churn that whipsaws the servo:
  - **Area gate** `YOLO_AREA_GATE_MULT` (4.0) — reject a detection whose area is >4× or <¼ the median of the last 5; don't reinit to it.
  - **Reinit debounce** `REINIT_COOLDOWN_S` (0.5 s) — rate-limit reinits; bypassed only when the lock is clearly lost (`center_dist > frame_diag × LOST_CENTER_FRAC` = 0.5).
- **Bbox-trust guard (bloat hold)** — when the ViT lock dissolves into an oversized box the centroid is garbage, so the servo holds instead of chasing it:
  - `BBOX_FREEZE_RATIO` (1.0) — bbox ≥ full frame area ⇒ ViT dissolved.
  - `BLOAT_HOLD_MULT` (3.0) — bbox > 3× the last trusted lock area ⇒ hold and force a re-detect.
- **Servo confidence floor** — ViT confidence < `SERVO_MIN_CONF` (0.25) holds the servo (`LOW-CONF-HOLD`) even while the detector is still confirming the target; without it, conf 0.15–0.4 with a fresh confirm was a blind zone that kept the servo chasing a barely-held (often ghost) lock. The tracker keeps updating and the PID resumes when confidence recovers.
- **Detector-gated trust** — if no detector has confirmed for `TRUST_TRACKER_S` (2.5 s) and ViT confidence < `TRACKER_TRUST_CONF` (0.4), hold the servo (`WAIT-YOLO`) rather than chase a phantom; high ViT confidence keeps firing even without a fresh detector confirm.
- **Hold really holds** — every hold state (`LOW-CONF-HOLD`, `WAIT-YOLO`, `BLOAT-HOLD`, low-confidence skip frames) retargets the follow worker to the *current* pose (`ServoFollower.hold()`). Previously a hold only stopped publishing new goals, so the worker kept gliding toward the last stale goal — the arm visibly chased a ghost for a beat after the lock had gone bad.

### Pixel-to-Degree Conversion

```
deg_per_px = CAMERA_FOV_DEG / frame_width          (same on both axes for square pixels)

dx = filtered_lead_x - frame_width/2   (positive = right)
dy = filtered_lead_y - frame_height/2  (positive = below)

yaw_step         = clamp(PID(soft_deadband(dx)) + VFF·vx·deg_per_px·dt,  ±5°)
pitch_correction = clamp(PID(soft_deadband(dy)) + VFF·vy·deg_per_px·dt,  ±5°)
```

### Tuning Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `VISION_MAX_WIDTH` | 640 | Downscale width for ViT + detectors (0 = off) |
| `FAST_LOOP_FPS` | 15 | Vision loop frequency (servo commands are decoupled; the worker wakes at ~33 Hz and coalesces tiny setpoint changes) |
| `CAMERA_FOV_DEG` | 60 | Horizontal FOV, for px→deg |
| `DEAD_ZONE_INNER_PCT` | 0.02 | True-zero band (servo rests) |
| `DEAD_ZONE_YAW_PCT` / `_PITCH_PCT` | 0.07 / 0.05 | Outer dead-zone edge (creep band ends) |
| `DEAD_ZONE_CREEP_GAIN` | 0.12 | Lazy-drift slope inside the creep band |
| `PID_YAW_KP` / `PID_PITCH_KP` | 0.015 / 0.02 | PID proportional gains (trim only — VFF is primary) |
| `PID_OUTPUT_MAX_DEG` | 5.0 | Max degrees per fire (yaw & combined pitch) |
| `AB_ALPHA` / `AB_BETA` | 0.6 / 0.2 | Alpha-beta position/velocity gains |
| `AB_GATE_PX` | 200 | Reject a centroid teleport beyond this residual |
| `AB_LEAD_S` | 0.20 | Velocity lead (cinematic "lead room" ahead of the target) |
| `VFF_GAIN` | 0.9 | Fraction of target velocity fed forward (primary command) |
| `VFF_MAX_DT_S` | 0.20 | Cap on per-fire dt for feedforward |
| `VFF_MOVING_MIN_PXS` | 40 | Target speed above which a centered target keeps panning |
| `SERVO_SMOOTH_TIME` / `SERVO_MAX_SPEED_DPS` | 0.32 / 55 | Pursuit profile (heavy, fluid-head) |
| `SACCADE_SMOOTH_TIME` / `SACCADE_MAX_SPEED_DPS` | 0.20 / 100 | Saccade profile (fast relocation) |
| `SACCADE_OFFSET_FRAC` / `SACCADE_EXIT_FRAC` | 0.22 / 0.12 | Saccade enter/exit thresholds (hysteresis — no speed-cap flip-flop at the boundary) |
| `SERVO_SUBSTEP_SLEEP` / `SERVO_SUBSTEP_MAX_DT_S` | 0.030 / 0.060 | Servo-worker wake period / maximum measured SmoothDamp step after a stall |
| `SERVO_COMMAND_MIN_DELTA` | 0.08 | Coalesce only tiny normalized setpoint changes; the final target is sent once |
| `TRACKING_GOAL_VELOCITY` | 0 (unlimited) | Explicitly written at session start to clear a stale hardware cap; SmoothDamp profiles own the speed envelope (150 steps/s ≈ 13°/s flattened every ease curve into a robotic crawl). `TRACKING_RETURN_VELOCITY` (200) caps only the return-to-zero glide |
| `TRACKING_ACCELERATION` | 30 | Hardware acceleration ramp |
| `PITCH_WEIGHT_BASE/ELBOW/WRIST` | 0.10 / 0.90 / 0.0 | Pitch distribution across joints |
| `ELBOW_PITCH_SIGN` | -1.0 | Elbow polarity (hardware reversed) |
| `YOLO_REDETECT_S` | 1.5 | Background re-detect interval |
| `YOLO_AREA_GATE_MULT` | 4.0 | Reject re-detect area outliers |
| `REINIT_COOLDOWN_S` | 0.5 | Min seconds between tracker reinits |
| `BBOX_FREEZE_RATIO` | 1.0 | Bbox ≥ frame ⇒ ViT dissolved (hold) |
| `BLOAT_HOLD_MULT` | 3.0 | Bbox > 3× trusted lock ⇒ hold |
| `CONFIDENCE_THRESHOLD` | 0.15 | Below this = low-confidence frame |
| `LOW_CONF_WINDOW` / `LOW_CONF_STOP_COUNT` | 15 / 8 | Sliding window: ≥8 low frames in the last 15 → stop (a consecutive counter was reset by every above-threshold flicker, letting ghost locks live forever) |
| `SERVO_MIN_CONF` | 0.25 | Confidence floor for firing the servo PID at all |
| `TRACKER_TRUST_CONF` / `TRUST_TRACKER_S` | 0.4 / 2.5 | Detector-gated trust (see above) |
| `YOLO_MAX_MISS` | 30 | Consecutive tracker misses before retry |
| `MAX_TRACK_DURATION_S` | 300 | Auto-stop timeout (5 min) |
| `_LOCAL_IMGSZ` | 320 | Local YOLO inference size (640 → 1.3–2.9 s, too slow) |

All knobs live in `hal/drivers/tracking/constants.py`. (The old dead `GIMBAL_*` / `EMA_ALPHA` proportional path was removed in the package split.)

### Servo Position Limits

| Joint | Min | Max |
|-------|-----|-----|
| base_yaw | -135 | 135 |
| base_pitch | -90 | 30 |
| elbow_pitch | -90 | 90 |
| wrist_pitch | -90 | 90 |

## Auto-Stop Conditions

| Condition | Action |
|-----------|--------|
| `confidence < 0.15` in ≥8 of the last 15 frames (sliding window) | Stop — lost target |
| Bbox shrinks below `DETECT_MIN_AREA_RATIO` | Stop — ghost-lock on a sliver |
| Bbox overflows frame + no detect for 3 s | Forced retry, then stop if unrecovered |
| No detector confirm for `STOP_NO_YOLO_S` (20 s) | Stop — ghost tracking |
| CSRT misses `YOLO_MAX_MISS` (30) after `MAX_TRACKING_RETRIES` (4) | Stop — object gone |
| Tracking duration > 5 minutes | Stop — timeout to save motor/CPU |
| GPIO-button or TTP223 single-click | Stop — explicit user attention-cancel |

Note: a large bbox (e.g. a person filling the frame) is **not** a stop condition — PID drives off the centroid, not bbox size, so a close object still tracks. When tracking ends the arm glides back to zero at tracking speed (no snap).

### Auto-stop on gateway/network disconnect

Object tracking is driven by remote vision updates from the agent/cloud. When the gateway WebSocket disconnects (cloud or internet loss), the device auto-stops any in-flight servo tracking — `runtimes/openclaw/service_ws.go` calls `hal.StopServoTracking()` → HAL `POST /servo/track/stop` (best-effort, guarded by `SetUpCompleted`). Without fresh remote updates, continued tracking would keep aiming the body at a stale target it can no longer correct, so it is stopped as a safety reflex. Local idle animation continues (the device stays "alive", doesn't freeze) and recovery (`/servo/track/stop`, stop/release) stays available. See `robots/lamp/SAFETY.md` → `## fail-safe states` (Network/gateway loss row, enforced).

## API Endpoints

All under `/servo/track`.

### GET /servo/track/targets — List suggested targets

```json
{"targets": ["person", "cup", "bottle", "glass", "phone", "laptop", ...]}
```

Detection is open-vocabulary via YOLOWorld (and YuNet for faces) — any text works, this list is just suggestions.

### POST /servo/track — Start tracking

`target` accepts either a single string or a list of candidate labels. When a list is passed, the first non-empty label is used. Useful when the caller (e.g. an LLM skill) is unsure which exact label will match.

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
  "target": "cup",
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

Manual re-init of the tracker with a new bbox without stopping the session (the background YOLO re-detect handles drift automatically; this is for callers that want explicit control).

```json
{"bbox": [250, 160, 75, 95], "target": "cup"}
```

## End-to-End Flow

### Happy path

```
1. User: "Lamp, follow the cup"
2. Agent calls POST /servo/track {"target": "cup"}
3. HAL internally:
   a. Freezes servos 0.3s and snapshots a sharp frame
   b. Detects "cup" (local YOLOv8n, or remote YOLOWorld) → bbox
   c. TrackerVit init uses the same frame + bbox (coordinates match)
   d. Starts the vision loop + servo worker
4. Servo pans smoothly to follow the cup, background YOLO corrects drift
5. User: "OK stop" → agent calls POST /servo/track/stop
6. Servo glides back to zero
```

### Auto-stop on lost

```
1. Object leaves frame or is occluded
2. TrackerVit confidence stays below 0.15 for most of the recent window (or ViT lock dissolves)
3. Background YOLO can't re-find it → after the guards trip → auto-stop
4. Arm returns to zero
5. Agent can notify user or re-issue the follow command
```

## Camera Stream Overlay

When tracking is active, the MJPEG stream (`/camera/stream`) draws:
- Green bounding box around the tracked object
- Target label above the box

## Web UI

Camera section shows:
- **Vision Tracking card** — target input, bbox input, Start/Stop/Status buttons
- **Stream badge** — "LIVE" or "TRACKING: {target}"
- **Confidence** — shown in tracking info panel
- **Polling** — status refreshes every 3 seconds

## Dependencies

- `opencv-python>=4.8.0` (already in `pyproject.toml`)
- `ultralytics` — local YOLOv8n inference
- `vittrack.onnx`, `yolov8n.pt`, `face_detection_yunet_2023mar.onnx` — checked into `hal/drivers/tracking/models/`
- `requests` (already in project)
- **YOLOWorld API** — DL backend at `{DL_BACKEND_URL}/detect/yoloworld` (open-vocab fallback only)

## Interaction with Other Systems

| System | During tracking | After tracking |
|--------|----------------|----------------|
| Servo idle animation | Suppressed (`_hold_mode`) | Resumed |
| `/servo/play` | Blocked by `_hold_mode` | Resumed |
| Sensing (face, motion) | Continues — shares camera | Continues |
| Camera stream overlay | Green bbox drawn | Normal stream |
| TTS | Continues normally | Continues normally |

## Performance Notes

- Fast-loop CPU floor on the Allwinner A523 is ViT inference + detector cost; the frame downscale (`VISION_MAX_WIDTH`) and local imgsz=320 are the main levers.
- Motion smoothness comes from the decoupled servo worker + SmoothDamp + velocity feedforward; the alpha-beta filter + reinit gating keep the goal itself stable so the follower isn't chasing noise.
- Small/far objects (e.g. a cup across the room) can exceed both local and remote detector resolution — a perception limit, not a control bug.

---

## Look-aim — pointing the head before a visual question captures

Separate from object tracking above, and driven by a different trigger.

The realtime `look` tool takes **no parameters**: it captures whatever the head currently faces
(`orchestrator.py` — *"the model just signals intent to look; the device grabs the current frame"*).
So a visual question — *"what am I holding?"* — could be answered confidently from a picture of a
wall. `hal/drivers/tracking/aim.py` centres the subject first.

| | |
|---|---|
| **Trigger** | the `look` tool firing — **not** ordinary conversation |
| **Scope** | yaw only |
| **Budget** | `HAL_LOOK_AIM_DEADLINE_S` (8 s); on expiry it captures from wherever it reached |
| **Disable** | `HAL_LOOK_AIM=false` |

Ordinary chat is untouched: the body stays still through listening and thinking as before. Only a
`look` call releases it, because that is the moment the device was explicitly asked to look at
something.

**The body is owned for the whole look.** From the moment the aim starts until the shutter closes,
`servo_ownership()` sets the same `_tracking_active` lock the vision tracker uses, which suppresses
**all** emotion servo animation (`routes/emotion.py`) and makes the animation loop drop any recording
in progress.

This is not optional polish. Emotion presets play **recorded** poses that are absolute on every
joint — including `wrist_roll` — so one arriving between the aim and the capture re-poses the head
entirely, and the frame shows wherever the animation parked it rather than the user. A "curious"
reaction landing mid-question is enough to capture the ceiling. `nudge()` preempts an animation that
is already playing, but not one dispatched afterwards, which is exactly the window the capture sits in.

The previous lock value is restored rather than cleared, so a look never ends a genuine
object-tracking session that was already running.

**Why the centring loop is yaw only.** The yaw sign is copied from the tracker's empirically
verified convention (`dx>0` → `base_yaw` increases). `AnimationService.nudge()` drives `base_pitch`,
whereas the tracker distributes pitch across base/elbow/wrist — so the pitch sign is **not**
validated on this path, and an inverted pitch is a bug this codebase has already hit once (see
`servo_follow.command_pid`).

The bearing restore in priority 3 is the exception, and it is safe for a specific reason: it sends an
**absolute** pose via `move_and_hold`, not a relative nudge. An absolute target has no sign to get
wrong. That is what lets a head left pointing at the floor recover its height — with yaw-only
correction it would sweep the floor in a circle no matter how right the direction was.

**Priority order:**

1. **Person visible** → centre it. Person box preferred over the face box: a held-up object often
   hides the face but rarely the whole body, and framing the person includes whatever they hold.
2. **Nothing visible, but a subject was confirmed at this pose seconds ago** → **hold and capture.**
   Treat the disappearance as *occlusion, not absence* — that is what a held-up object looks like to
   a detector, and turning away would abandon the very thing the user asked about.
3. **Nothing visible and nothing seen recently** → go to the remembered **pose** — direction and
   posture together — in a single absolute move. This used to advance in `BEARING_STEP_DEG` hops,
   re-detecting between them so it could not pass over someone standing en route; the hops were
   dropped because the lens sees ~110°, so anyone in between is already in frame before the head
   moves at all. They bought no coverage and cost a detect plus a settle each, roughly a second per
   hop out of the aim's own budget.
4. **Deadline** → capture from wherever the head reached. It never sweeps here.

Every move goes through `nudge()`, so `SAFETY.md`'s `max_speed` stretches the move rather than being
bypassed to meet the deadline. A physical-button single click aborts it (`button_actions.py`), since
that gesture means "stop moving and pay attention to me".

#### Which detection counts as "the person asking"

A `person` box is not enough. The detector's global floor is `DETECT_MIN_CONFIDENCE = 0.15`,
deliberately loose because it is tuned for the **tracker**, where losing a lock on a phone at an odd
angle costs more than a false positive. Aiming wants the opposite trade — a false positive turns the
lamp at a wall — so the aim applies its own two gates:

| gate | default | rejects |
|---|---|---|
| `HAL_LOOK_AIM_MIN_PERSON_HEIGHT_FRAC` | 0.15 | a colleague across the room (measured 0.10 of frame height) |
| `HAL_LOOK_AIM_MIN_FACE_HEIGHT_FRAC` | 0.08 | a spurious far face (measured 0.035) |
| `HAL_LOOK_AIM_MIN_CONFIDENCE` | 0.5 | low-confidence noise, e.g. a person rendered on a monitor |

Size is measured on **height**, not area or width: a close subject is routinely clipped left or right
by the frame edge, but their apparent height still scales with distance.

A rejected detection is reported as **no subject at all**, not as a target — so the aim falls through
to hold-or-consult-the-bearing rather than turning to a stranger at the other end of the room. Faces
come from YuNet, which enforces its own threshold and reports no confidence, so only the height gate
applies there.

#### Self-calibrating pixels-to-degrees

The aim does **not** trust a fixed FOV constant. It measures degrees-per-`dx_frac` from what its own
last move actually achieved, and uses that for the next correction; `HAL_LOOK_AIM_FOV_DEG` (100°) is
only the first-step guess before any measurement exists.

This exists because no single constant can be right. The lens is a fisheye: the same device measured
**91° near the frame centre and 229° at the edge**. A constant tuned for the centre crawls at the
edge (four iterations still not centred, then a timeout); one tuned for the edge overshoots the
centre and oscillates.

Guards, because dividing a small shift by a small move turns detector jitter into a wild scale: a
step is ignored unless the head moved >3° and the subject shifted >0.02 of frame, and unless the
shift went the **same** direction as the correction — a wrong-way shift means the subject walked or
the detector jumped to something else, which is not a measurement of optics. The result is clamped to
40–250° and damped by `SCALE_SAFETY` (0.7), deliberately biased low: the measurement is taken at the
current eccentricity but spent at a smaller one, and undershoot costs a step where overshoot
oscillates.

#### Capture timing

Two costs were paid inside the aim's budget before being moved out of it:

- **Detector warm-up.** The first `detect()` loads the model lazily and cost ~9 s on device — enough
  on its own to blow the deadline and the realtime turn's watchdog. It is now pre-warmed on a
  background thread at HAL start (`server.py`), so the first real look runs at the same speed as
  every later one.
- **Stale frames.** Reading `last_frame` straight after a move returns the **pre-move** image, so the
  next correction is computed from a pose the head has already left. On device that produced six
  identical +12.3° corrections with `dx` frozen at 0.241 while the head travelled 61°. The aim now
  holds the camera consumer for the whole aim (without one the device does not capture at full FPS)
  and requires a frame stamped after the servo settled. **No fresh feedback, no move.**

The shutter itself uses `capture_still`, which freezes the servos and waits for quiet. Its settle
scales with the size of the last correction (0.3 s base, +0.0067 s/deg, capped at 0.5 s), because an
aim that exits on its deadline does so immediately after a large swing and a lamp arm is still
ringing past a flat 300 ms — that was the difference between sharp captures on centred aims and
blurred ones on timed-out aims. The cap is deliberately tight: this delay is paid before the user
hears an answer.

#### Debugging a look

Off by default; when disabled every hook is a single cached bool check, so it costs nothing to leave
in place.

```bash
HAL_LOOK_DEBUG=true          # per-look trace dirs under drivers/tracking/look_logs/
HAL_LOOK_DEBUG_FRAMES=false  # keep the trace, skip the per-step JPEGs
```

Each look writes `<timestamp>_<status>/` containing:

| file | what it answers |
|---|---|
| `step_NN_*.jpg` | what the detector locked onto each iteration — green box, green line at box centre, red line at frame centre. The gap between the lines **is** `dx`. |
| `capture.jpg` | the frame actually sent to the model |
| `result.json` | the decision trail: per step `saw` / `dx_frac` / `conf` / `scale` / commanded yaw / resulting pose, plus the bearing consulted |
| `profile.json` | stage timings, and `waiting_on_model_ms` |

The status in the directory name (`OK_realtime_handled`, `OK_delegated`, `OK_fallback`) says which
path answered the turn, so a bad answer can be attributed before opening anything.

`waiting_on_model_ms` is the one to read first: it is total minus everything the device did itself,
and it separates "the lamp is slow" from "the lamp finished in 2 s and then waited 24 s for the
model". Sub-stages are nested inside their roll-up and excluded from the device total, so the
residual is honest. The same numbers appear on one `LOOK-PROFILE` log line per look.

### Search sweep — asked for, never inline

Distinct from the look-aim, and deliberately kept off the capture path. The aim runs inside a live
turn under a deadline; a sweep takes seconds, which is exactly the dead air that design avoids. So a
sweep is only entered where the time is affordable:

- the user asks outright — *"where are you?"*, *"can you find me?"* (`skills/servo-control`)
- they accept an offer after a failed look — *"I can't see it. Want me to look around?"*

`POST /servo/search` — sweeps and stops on the first subject seen.

**Ordering is the whole trick.** Stops are seeded from the remembered bearing and expand outward
(`seed`, `seed±45°`, `seed±90°`, …) rather than sweeping left-to-right, so the likely place is checked
first. That is what usually turns a multi-second sweep into a single stop.

Steps are `STEP_DEG` (45°), deliberately **smaller than the camera FOV** so tiles overlap — stepping
by a full FOV would leave seams where someone straddling two tiles is missed by both. Stops are
clamped to the ±135° mechanical range, and the head is given `SETTLE_S` to stop ringing before each
frame is read, since a moving head yields a blurred frame and a detector that misses what is in view.

Aborted by the physical button like the aim, and it never sweeps while the camera is disabled — a
search is a lot of conspicuous movement to perform when the user has asked the device not to look.

> Not built: an LED cue while sweeping. Transient LED state lives behind the route request models, so
> driving it from here would mean HTTP loopback (which this codebase avoids) or duplicating the
> restore bookkeeping — and a cue that fails to restore would strand the lamp's LED. Worth doing
> properly rather than partially.

### Speaking while it searches

A lamp that silently swivels away mid-question looks broken. One that says *"where are you?"* while
doing it reads as trying to help.

os-server owns the phrases, the language resolution and the WAV cache
(`system/lib/i18n/fillers.go`, pools `look_searching` / `look_found` / `look_capturing`); HAL only
decides **when**, via `POST /api/sensing/filler` with `{"pool": "..."}`.

| State | When | Default |
|---|---|---|
| `look_searching` | the first step toward the remembered bearing | **on** (`HAL_LOOK_AIM_SPEAK`) |
| `look_found` | a subject appears **after** a search was announced | on (same flag) |
| `look_capturing` | the aim had work to do before the shutter | on (`HAL_LOOK_AIM_SPEAK_CAPTURE`) |

The gating matters more than the phrases. **Nothing is said when the subject is already centred** —
that capture completes in a few hundred milliseconds, so every phrase here is conditional on the aim
having actually moved. *"There you are"* only fires as the resolution of an announced search, never
on its own. Searching announces **once**, not per step. And the capture line fires only when the aim
had work to do, which is what keeps it from prefixing every visual question.

A fast, silent, correct capture is already the good outcome — speech is reserved for the moments the
user is genuinely left waiting.

### Remembered user bearing

`hal/drivers/tracking/user_bearing.py` folds sightings into one decaying estimate at
`/var/lib/hal/user_bearing.json` (`HAL_USER_BEARING_PATH`). One place, not a histogram — the lamp
only ever needs one pose to return to.

**It stores a full servo pose, not a single angle** (schema v2; a v1 file migrates and keeps its
learned direction). `bearing_deg` remains as the yaw component so callers that only want a direction
need not know joint names, and it is *derived from* `pose["base_yaw.pos"]` so the two can never
disagree. Yaw alone is not enough to look at someone: pitch is spread across base/elbow/wrist, so a
head left pointing at the floor sweeps the floor in a circle no matter how right the yaw is. Each
joint gets its own EMA at the same rate as the yaw; a relocation replaces the pose outright rather
than averaging, since the old posture describes the old place.

Sightings reach it two ways:

- **From a look aim**, when the subject ends within **2%** of frame centre — tighter than the aim's
  own framing tolerance, and deliberately so: at frame centre the servo position **is** the bearing,
  with no pixel→angle conversion and therefore no dependency on the disputed camera FOV constant.
- **From the passive sampler** (`bearing_sampler.py`), every `HAL_BEARING_SAMPLE_INTERVAL_S` (300 s).
  The aim-only path recorded roughly two samples a day against a six-hour confidence half-life — it
  decayed faster than it learned, so the one thing that rescues a look when nobody is visible was
  never confident enough to be consulted. The sampler **never moves the lamp**: it reads a frame and
  the current servo positions, and recovers the bearing arithmetically as `yaw + dx × scale`.

The sampler declines rather than guess. Horizontal offset is tolerated only to
`HAL_BEARING_SAMPLE_MAX_DX_FRAC` (0.25), because that correction leans on the very FOV constant the
aim exists to avoid trusting. The **posture** is recorded only when the subject is also vertically
centred (`HAL_BEARING_SAMPLE_MAX_DY_FRAC`, 0.15) — pitch cannot be corrected arithmetically here, so
a subject high or low in frame means the current pitch is *not* looking at them and storing it would
teach a posture aimed at the floor. It also skips while the body is aiming or tracking, while the
camera is disabled, and takes the detector lock non-blocking so a user's question never waits on it.
It applies the same size and confidence gates as the aim.

Each sample writes an annotated frame to `/var/lib/hal/snapshots/sensing_bearing/`
(`HAL_BEARING_SNAPSHOT`, newest 30 kept, oldest evicted) — **including the detections it rejected**,
labelled with why, since "it ignored a far stranger" and "it saw nothing" look identical in the
estimate. Servable at `GET /api/sensing/snapshot/sensing_bearing/<name>`.

Angles are averaged **linearly, not circularly**: `base_yaw` is a bounded ±135° servo range that does
not wrap, so a circular mean would be wrong at the extremes.

Outliers are damped rather than accepted — someone crossing the room must not flip the estimate — but
`OUTLIER_STREAK` consecutive far sightings are treated as a genuine relocation and accepted wholesale.

**Consumed by aim priority 3** (above) once confidence passes `MIN_BEARING_CONFIDENCE`. Inspect it
to check the maths and the sign:

```bash
curl -s localhost:5001/servo/bearing     # includes the full pose
cat /var/lib/hal/user_bearing.json
```

A `known` bearing with an empty `pose` means the estimate predates the pose schema and has not been
re-sighted yet: a search will restore direction but not head height until the next sighting fills it
in.

`bearing_deg` should settle near where the user actually sits. An estimate sitting **mirrored about
zero** means the yaw sign is inverted — the failure this file is most exposed to, because it is
open-loop and nothing corrects it.

### Noticing that the lamp has been moved

The bearing is stored in **lamp-relative** coordinates, so picking the lamp up or rotating it on the
desk invalidates it instantly — while the file still looks perfectly valid.

Nothing on this device can observe that directly:

| Approach | Why not |
|---|---|
| IMU / accelerometer | none fitted — absent from the BOM and from HAL |
| Servo feedback | `base_yaw` measures the head against the **base**. Rotating the whole lamp moves the world, not the joint. |
| Reboot as a hint | weak both ways — lamps reboot without moving, and move without rebooting |

So it is **inferred from failed predictions**: when aim priority 3 turns to the remembered bearing
and finds nobody, that is a miss. `PREDICTION_MISS_LIMIT` misses drops the estimate, and it rebuilds
from live sightings.

Three guards keep ordinary life from looking like a relocation:

- **A single miss is not enough** — the user may simply be out of the room.
- **A hit resets the streak**, so occasional absences never accumulate.
- **Misses must be clustered** (`MISS_STREAK_WINDOW_S`). A moved lamp fails every attempt from the
  moment it moved; a user who is sometimes in another room produces isolated misses spread over
  weeks. Without the window those become indistinguishable once enough time passes.

And a miss is only ever counted when the lamp **actually looked and found nothing**. Camera disabled,
no frame, deadline, button abort, and the occlusion hold all return without scoring — privacy mode in
particular must never erase where the user sits.

This self-heals for **any** cause (lamp moved, furniture rearranged, user changed desk) without ever
needing to know which one happened.

#### How big a move has to be before anything needs to detect it

Most moves never reach the miss-counting above, because the estimate corrects itself:

| Lamp rotated by | What corrects it |
|---|---|
| **< half the camera FOV** (~30°) | the user is **still in frame** at the stale bearing, so the aim finds and centres them anyway — and that sighting records the new correct yaw. Plain EMA pulls the estimate over. **Nothing detects the move; nothing needs to.** |
| up to `OUTLIER_DEG` (45°) | not visible from the stale bearing, but found while stepping toward it. The sighting is still under the outlier threshold, so it is folded in at full weight. |
| beyond `OUTLIER_DEG` | sightings look like outliers, so `OUTLIER_STREAK` consecutive ones are accepted as a relocation. |
| far enough that the user is never found | the miss streak drops the estimate and it rebuilds from scratch. |

So the machinery above is only for the **last** case. A lamp nudged on the desk is handled by the
estimate's own update rule, which is why the small case is also the quietest — the lamp never learns
it moved, and does not need to.

**Inspect and reset:**

```bash
curl 127.0.0.1:5001/servo/bearing              # {"known":true,"bearing_deg":-18.5,...}
curl -X POST 127.0.0.1:5001/servo/bearing/reset
```

The reset is also wired to speech via `skills/servo-control` — *"I moved you"*, *"you're in a new
place"*. Automatic detection needs several failures before acting, which is right for avoiding false
positives but slow when the user already knows the lamp moved.
