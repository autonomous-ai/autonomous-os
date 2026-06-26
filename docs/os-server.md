# OS Server API — Documentation

> OS Server (Go, Gin framework) runs on port 5000.

## OS Server Endpoints (Go, :5000)

### Health

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/health/live` | Liveness probe |
| GET | `/api/health/readiness` | Readiness probe (agent gateway connected?) |

### System

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/system/info` | CPU, RAM, temp, uptime, version, agent status (name/connected/emotion/version/uptime) |
| GET | `/api/system/network` | WiFi SSID, IP, signal, internet status |
| GET | `/api/system/dashboard` | Aggregated snapshot (agent + config + HW) |

### Device Setup

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/device/setup` | Configure WiFi + LLM + channel + MQTT (async, returns immediately) |
| POST | `/api/device/channel` | Change messaging channel |

### Device Timezone

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/device/timezone` | Active IANA zone + selectable zone list (admin-gated) |
| POST | `/api/device/timezone` | Apply an IANA zone (admin-gated) |

**GET response** (`data`):
```json
{
  "current": "Asia/Ho_Chi_Minh",
  "zones": ["UTC", "Asia/Ho_Chi_Minh", "..."]
}
```

- `current` is read live from `/etc/timezone`, falling back to resolving the `/etc/localtime` symlink, then the `timezone` field in `config/config.json`.
- `zones` comes from `timedatectl list-timezones`, falling back to a walk of `/usr/share/zoneinfo`, then a built-in common list.

**POST request body:**
```json
{ "timezone": "Asia/Ho_Chi_Minh" }
```

The zone is validated against `/usr/share/zoneinfo`; an unknown zone returns HTTP 400. On success the server: repoints the `/etc/localtime` symlink at the zone's tzdata file, writes `/etc/timezone` (Debian-style, trailing newline), runs `timedatectl set-timezone <tz>` best-effort (non-fatal if absent), and persists `timezone` to `config/config.json`.

The change takes effect **without a HAL restart** — HAL's clock helpers (`os/hal/clock.py`) read `/etc/timezone` fresh on every call.

Config field: `timezone` in `config/config.json` (IANA zone string, omitempty) — a record of the applied zone. The OS files (`/etc/timezone` + `/etc/localtime`) are the source of truth.

### Network

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/network` | Scan WiFi networks |
| GET | `/api/network/current` | Current SSID + IP |
| GET | `/api/network/check-internet` | Check internet connectivity |

### Guard Mode

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/guard/enable` | Enable guard mode |
| POST | `/api/guard/disable` | Disable guard mode |
| GET | `/api/guard` | Check guard mode status (returns `{"guard_mode": true/false}`) |
| POST | `/api/guard/alert` | Manually broadcast alert to all OpenClaw chat sessions |

**Alert request body:**
```json
{
  "message": "Intruder detected in living room",
  "image": "<base64 JPEG, optional>"
}
```

When guard mode is ON, `presence.enter` and `motion` sensing events are additionally broadcast to ALL OpenClaw chat sessions (Telegram DMs + groups) via `chat.send` RPC. Normal sensing flow (emotion, servo, TTS) continues unchanged.

Config field: `guard_mode` in `config/config.json` (bool, default `false`). The OpenClaw agent can also toggle guard mode via the `guard` skill.

### Sensing

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/sensing/event` | Receive sensing event from HAL |
| POST | `/api/mood/log` | Log user mood (called by agent via Mood skill) |
| POST | `/api/monitor/event` | Push an event directly to the monitor bus (used by HAL for sound tracker state) |

> **Note:** Stranger visit tracking (stats, persistence) is handled by **HAL** (port 5001) at `GET /face/stranger-stats`. See [sensing-behavior.md](sensing-behavior.md#stranger-visit-tracking) for details.

**Request body:**
```json
{
  "type": "voice_command|voice|web_chat|motion|sound|presence.enter|presence.leave|presence.away|light.level|motion.activity",
  "message": "...",
  "image": "<base64 JPEG, optional>"
}
```

**Event types:**

| Type | Source | Has image? | Description |
|------|--------|-----------|-------------|
| `voice_command` / `voice` | Mic (Deepgram STT) | No | Voice command |
| `web_chat` | Web Monitor `/chat` UI | Yes (file/clipboard attach) | Typed message from web monitor — TTS suppressed (reply rendered in UI), no physical wake, no opening filler |
| `motion` | Camera (frame diff) | Yes (large motion) | Motion detected |
| `presence.enter` | Camera (InsightFace recognition) | Yes (bbox-annotated JPEG) | Face detected — friend or stranger classified |
| `presence.leave` | Camera (3 consecutive ticks without face) | No | Person left |
| `light.level` | Camera (mean brightness) | No | Significant ambient light change (>30/255) |
| `sound` | Mic (RMS energy) | No | Loud noise |
| `presence.away` | PresenceService (15 min no motion) | No | No one around for 15+ min — device going to sleep |
| `motion.activity` | MotionPerception (while PRESENT) | No | Activity detected while user is present — emotional actions logged via Mood skill |

**Processing flow:**
1. `voice_command` or `voice` + local intent enabled → match intent → execute directly (~50ms). `web_chat` skips local intent (typed text ≠ wake-word voice).
2. No match → forward to OpenClaw via WebSocket `chat.send`
3. If event has `image` → call `SendChatMessageWithImage` → send image with text for AI vision analysis. For `web_chat`, attached image is saved to `/tmp/web-chat-*.jpg` and tagged `[image: <path>]` so the agent can reference it (e.g. for face enrollment).
4. `web_chat` runs are tagged via `MarkWebChatRun(runID)` so the SSE handler suppresses TTS at lifecycle end — reply is rendered in the web UI only.

### OpenClaw

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/openclaw/status` | WS connection status; includes `uptime` (OS server WS uptime) and `agentUptime` (OpenClaw process uptime, survives OS server restarts) |
| GET | `/api/openclaw/events` | SSE stream real-time events |
| GET | `/api/openclaw/recent` | 100 most recent events (ring buffer) |

---

## HAL Endpoints (Python FastAPI, :5001)

Accessed via nginx proxy: `/hw/*` → `127.0.0.1:5001`

### Servo (5-axis Feetech)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/servo` | Recordings + animation state |
| POST | `/servo/play` | Play animation (idle, curious, nod, headshake, happy_wiggle, sad, excited, shock, shy, scanning, wake_up, music_groove, listening, thinking_deep, laugh, confused, sleepy, greeting, acknowledge, stretching). Idle auto-plays on boot. |
| POST | `/servo/move` | Send joint positions with smooth interpolation |
| POST | `/servo/release` | Disable torque on all servos |
| GET | `/servo/position` | Current servo positions |
| GET | `/servo/aim` | List aim directions |
| POST | `/servo/aim` | Aim device head (center, desk, wall, left, right, up, down, user) |
| GET | `/servo/track/targets` | List suggested target names for YOLOWorld detection |
| POST | `/servo/track` | Start tracking — `{"target":"cup"}` (auto-detect) or `{"bbox":[x,y,w,h]}`. See [vision-tracking.md](vision-tracking.md) |
| POST | `/servo/track/stop` | Stop current tracking session |
| GET | `/servo/track` | Get tracking status (active, target, bbox, confidence) |
| POST | `/servo/track/update` | Re-initialize tracker with new bounding box |

### LED (64 WS2812, 8x5 grid)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/led` | LED strip info |
| GET | `/led/color` | Current LED color |
| POST | `/led/solid` | Fill entire strip with one color |
| POST | `/led/paint` | Set individual pixels (array up to 64) |
| POST | `/led/off` | Turn off all LEDs |
| POST | `/led/effect` | Start effect (breathing, candle, rainbow, notification_flash, pulse) |
| POST | `/led/effect/stop` | Stop running effect |

### Camera

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/camera` | Availability + resolution |
| GET | `/camera/snapshot` | Capture 1 JPEG frame. `?save=true` saves to timestamped file, returns JSON `{"path":"..."}` |
| GET | `/camera/stream` | MJPEG live stream (downscaled + throttled) |

### Audio

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/audio` | Audio device availability |
| POST | `/audio/volume` | Set volume (0-100%) |
| GET | `/audio/volume` | Get volume |
| POST | `/audio/play-tone` | Play test tone |
| POST | `/audio/record` | Record WAV |
| POST | `/audio/play` | Play music by query. Body: `{"query":"song artist","person":"name"}`. `person` optional — enables per-user history. Fires a short cached TTS cue ("On it.", "Coming up.", …) before yt-dlp resolve so the device sounds responsive while ffmpeg loads. Cue is suppressed when speaker muted, TTS busy, music already playing, or VoiceService is mid-STT-session. |
| POST | `/audio/stop` | Stop current music playback |
| GET | `/audio/status` | Current playback status (playing, title, elapsed) |
| GET | `/audio/history` | Music play history. Query: `?person=name&date=YYYY-MM-DD&last=50`. `person` filters per-user; omit for shared. |

### Emotion

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/emotion` | Combined expression: servo + LED + display eyes |

15 emotions: curious, happy, sad, thinking, idle, excited, shy, shock, listening, laugh, confused, sleepy, greeting, acknowledge, stretching

### Scene

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/scene` | List scene presets |
| POST | `/scene` | Activate scene (reading, focus, relax, movie, night, energize) |

### Presence

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/presence` | Current state (present/idle/away) |
| POST | `/presence/enable` | Enable auto presence control |
| POST | `/presence/disable` | Disable auto presence (manual mode) |

### Face (friend enrollment)

Requires sensing with camera (InsightFace). Enrolled person JPEGs persist under `/root/local/users/{label}/` by default, or under `HAL_USERS_DIR` if set. Each person's folder contains a `metadata.json` with `telegram_username` and `telegram_id` for DM targeting.

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/face/enroll` | Body: `image_base64`, `label`, `telegram_username`?, `telegram_id`? — save photo, train friend embeddings, persist Telegram identity |
| GET | `/face/status` | `enrolled_count`, `enrolled_names` |
| POST | `/face/remove` | Body: `label` — remove one person (404 if unknown) |
| POST | `/face/reset` | Clear all enrolled persons and photos on disk |

### User (per-user data)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/user/info?name=X` | User metadata: `name`, `is_friend`, `telegram_id`, `telegram_username`. Defaults to `"unknown"` if name omitted. Auto-creates folder. |

> Wellbeing activity history lives on the OS server HTTP API (port 5000). See `POST /api/wellbeing/log` and `GET /api/openclaw/wellbeing-history` — entries are JSONL under `/root/local/users/{user}/wellbeing/YYYY-MM-DD.jsonl` with schema `{ts, seq, hour, action, notes}` (action ∈ `drink`/`break`/`sedentary`/`emotional`). HAL no longer hosts wellbeing endpoints.

### Display (GC9A01 1.28" round LCD)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/display` | Current state (mode, expression) |
| POST | `/display/eyes` | Set eye expression + pupil position |
| POST | `/display/info` | Switch to info mode (text/subtitle) |
| POST | `/display/eyes-mode` | Switch back to eyes mode (default) |
| GET | `/display/snapshot` | Current frame as JPEG |

11 expressions: neutral, happy, sad, curious, thinking, excited, shy, shock, sleepy, angry, love

### Voice

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/voice/start` | Start voice pipeline (Deepgram STT + TTS) |
| POST | `/voice/stop` | Stop voice pipeline |
| POST | `/voice/speak` | TTS — convert text to speech. Body fields: `text`, `voice?`, `interruptible?`, `provider?`, `tts_api_key?`, `tts_base_url?`, `cached?` (use WAV cache, render+save on miss), `prerender?` (render+save without playing — boot warmup) |
| GET | `/voice/status` | voice_available, voice_listening, tts_available, tts_speaking |

### System

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Hardware driver availability |

---

## Response Format

OS Server (Go):
```json
{"status": 1, "data": {...}, "message": null}   // success
{"status": 0, "data": null, "message": "error"}  // failure
```

HAL (Python): FastAPI standard JSON responses.

## Startup

1. OS Server starts Gin on :5000
2. Reads `config/config.json`
3. If `SetUpCompleted`:
   - Connect OpenClaw WebSocket
   - Connect MQTT
   - Start ambient behaviors
4. If not yet set up: wait for `POST /api/device/setup`

## Local Intent Matching

When receiving a `voice_command` or `voice` event, the OS server checks local intent first (~50ms):

| Command | Action |
|---------|--------|
| "turn on light" | `/led/solid` warm + happy emotion |
| "turn off light" | `/led/off` + idle emotion |
| "reading mode" | scene:reading |
| "focus mode" | scene:focus |
| "relax" | scene:relax |
| "movie mode" | scene:movie |
| "goodnight" | scene:night + sleepy emotion |
| "brighter" | scene:energize |
| "happy" | emotion:happy |
| "sad" | emotion:sad |
| "volume up" | volume 80 |
| "volume down" | volume 30 |
| "mute" | volume 0 |

No match → forward to OpenClaw.
