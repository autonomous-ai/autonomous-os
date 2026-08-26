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
| GET | `/api/system/ota-security` | OTA trust posture from the bootstrap worker: `legacy` vs `verified`, pinned key fingerprint, last metadata fetch (see `bootstrap-ota.md`) |

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

The change takes effect **without a HAL restart** — HAL's clock helpers (`hal/clock.py`) read `/etc/timezone` fresh on every call.

Config field: `timezone` in `config/config.json` (IANA zone string, omitempty) — a record of the applied zone. The OS files (`/etc/timezone` + `/etc/localtime`) are the source of truth.

### Network

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/network` | Scan WiFi networks |
| GET | `/api/network/current` | Current SSID + IP |
| GET | `/api/network/check-internet` | Check internet connectivity |

**Connectivity monitor** (`system/network/service.go`, started once
`SetUpCompleted` flips true). Pings `8.8.8.8` every 5s — interface-agnostic, so a
device online over ethernet is seen as online. After 5 consecutive failures it
raises the `Connectivity` LED state; after 10 (~50s) it escalates to a WiFi
reconnect (restart `wpa_supplicant@wlan0`, bounce the interface), and after 5
failed reconnects (~10 min) it reboots the device.

That escalation is a **WiFi** recovery path, so it is skipped when WiFi is not the
link in question — otherwise a wired device would reboot itself every ~10 minutes
for the length of an upstream outage it plays no part in. It is skipped when
either: no SSID is on file (the device was provisioned over ethernet — see
`setupWired` in `docs/setup-flow.md`), or the default route belongs to another
interface (traffic is leaving over the cable). A genuinely dropped WiFi link
leaves *no* default route and `PrimaryInterface()` falls back to `wlan0`, so the
outage the escalation exists for still passes the guard.

### Guard Mode

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/guard/enable` | Enable guard mode |
| POST | `/api/guard/disable` | Disable guard mode |
| GET | `/api/guard` | Check guard mode status (returns `{"guard_mode": true/false}`) |
| POST | `/api/guard/alert` | Manually broadcast alert to all OpenClaw chat sessions |

All guard endpoints require administrator authentication for network callers.
Device-local callers on strict loopback, including HAL and the agent runtime, are
allowed so internal guard-mode operation remains available.

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

> **Note:** Stranger visit tracking (stats, persistence) is handled by **HAL** (port 5001) at `GET /face/stranger-stats`. See [sensing-behavior.md](../robots/lamp/docs/sensing-behavior.md#stranger-visit-tracking) for details.

**Request body:**
```json
{
  "type": "voice_command|voice_followup|voice|web_chat|mqtt_chat|motion|sound|presence.enter|presence.leave|presence.away|light.level|motion.activity",
  "message": "...",
  "image": "<base64 JPEG, optional>"
}
```

**Event types:**

| Type | Source | Has image? | Description |
|------|--------|-----------|-------------|
| `voice_command` / `voice_followup` / `voice` | Mic (Deepgram STT) | No | `voice_command` is wake-word confirmed; `voice_followup` is authorized by the short wake-word focus window; `voice` is ambient STT |
| `web_chat` | Web Monitor `/chat` UI | Yes (file/clipboard attach) | Typed message from web monitor — TTS suppressed (reply rendered in UI), no physical wake, no opening filler |
| `mqtt_chat` | MQTT `kind:"chat.send"` (phone app) | Yes (image + file) | Same handling as `web_chat` in every gate (`sensingmsg.IsChat`); separate type only so the Flow Monitor badge shows the origin. `speak:true` forwards as `voice` instead |
| `motion` | Camera (frame diff) | Yes (large motion) | Motion detected |
| `presence.enter` | Camera (InsightFace recognition) | Yes (bbox-annotated JPEG) | Face detected — friend or stranger classified |
| `presence.leave` | Camera (3 consecutive ticks without face) | No | Person left |
| `light.level` | Camera (mean brightness) | No | Significant ambient light change (>30/255) |
| `sound` | Mic (RMS energy) | No | Loud noise |
| `presence.away` | PresenceService (15 min no motion) | No | No one around for 15+ min — device going to sleep |
| `motion.activity` | MotionPerception (while PRESENT) | No | Activity detected while user is present — emotional actions logged via Mood skill |

**Processing flow:**
1. `voice_command`, `voice_followup`, or `voice` + local intent enabled → match intent → execute directly (~50ms). `voice_followup` has the same user priority as `voice_command`; `web_chat` / `mqtt_chat` skip local intent (typed text ≠ wake-word voice).
2. Ambient turn floor: `motion.activity`, `emotion.detected`, `speech_emotion.detected`, `sound`, `presence.away`, `light.level` are dropped when the last agent turn created by this handler (any type) was less than `sensing_turn_floor_s` seconds ago (config key, default `120`, `0` disables; guard mode bypasses). One cross-type floor on top of HAL's independent per-type gates — a burst of different event types costs at most one agent turn per window. Dropped events surface as `sensing_drop` (reason `ambient_floor`) in the Flow Monitor.
3. No match → forward to OpenClaw via WebSocket `chat.send`
4. If event has `image` → call `SendChatMessageWithImage` → send image with text for AI vision analysis. For chat types (`web_chat` / `mqtt_chat`), attached image is saved to `/tmp/web-chat-*.jpg` and tagged `[image: <path>]` so the agent can reference it (e.g. for face enrollment).
5. Chat runs (`web_chat` / `mqtt_chat`) are tagged via `MarkWebChatRun(runID)` so the SSE handler suppresses TTS at lifecycle end — reply is rendered in the chat UI only (web SSE, or MQTT `chat.event` stream).

### OpenClaw

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/agent/status` | WS connection status; includes `uptime` (OS server WS uptime) and `agentUptime` (OpenClaw process uptime, survives OS server restarts) |
| GET | `/api/agent/events` | SSE stream real-time events |
| GET | `/api/agent/recent` | 100 most recent events (ring buffer) |
| POST | `/api/agent/speech/cancel` | Physical cancel gesture (single click, called by HAL — loopback-only auth so the button works without a login). Silences every turn currently in flight and stops HAL playback (`StopTTS`, which also clears the pre-synthesised speak-queue). The turns are **not** aborted: they keep running, their tools still fire, and their text still reaches web chat and history — they only lose the speaker. Implemented as a monotone unix-ms watermark (`speechWatermarkMs`): `deliverTTS` drops any reply whose turn was created at or before the mark and logs a `tts_cancelled` flow event. Turn age comes from the runID — device ids end in their creation stamp (`device-chat-7-<unix-ms>`, 13 digits), channel ids (`tg-<messageID>`) have none and fall back to the first time speech was requested for that run. Because new turns are always on the far side of the mark, the user can click and immediately speak again while an older backlog drains silently; the watermark never needs clearing. The same mark also drops the turn's `[HW:]` markers in `fireHWCall` — servos and LEDs stop too, since a device that keeps moving after being told to stop reads as ignoring the user. The run id is put through `resolveRunID` first: the TTS path already holds the device id while HW dispatch may still carry the raw backend UUID for the same turn, and judging them separately muted the reply while the markers fired anyway. `/dm`, `/broadcast` and `/speak` are exempt (the gate sits after them): the click means "stop talking to me" and must not swallow a reply addressed to a Telegram user. |
| POST | `/api/agent/restart` | "Start + enable + restart" recovery for the active runtime. Steps: (1) best-effort `systemctl enable <unit>` — where `<unit>` is picked from a runtime→unit map (`openclaw`, `hermes-gateway`, `picoclaw`, `codex`, `claudecode`, `opencode`) — so the fix survives a reboot; (2) `agentGateway.RestartAgent()` which resolves to `systemctl restart <unit>` and thus STARTS the service even if it was stopped. Response `{backend, enabled}`. Used by the Overview's Agent Gateway card to recover a gateway that was stopped+disabled, without SSH. Internal restart callers (config refresh, migration) still bypass the enable step. |

---

## Device Ops Alerts (outbound → bff-campaign-service)

The device sends **operational / maintainer alerts about its own actions** to
`POST {llm_base_url}/alert` (i.e. `/api/v1/ai/v1/alert` on bff-campaign-service),
authenticated with the device's lobster API key (`Authorization: Bearer <llm_api_key>`).
bff-campaign-service holds the Telegram bot token + destination chat and relays the
text to a fixed maintainer chat — the token is **never on the device or in this
public repo**. Implemented in `system/lib/alert`.

**Privacy & data scope:** these alerts report **only device actions and state
changes** — never end-customer content. No chat messages, no personal data are
captured. They exist for **product improvement and troubleshooting only**. Each
alert carries device identity (label, MAC, SSID, IP, component versions) plus the
action outcome below.

**What fires an alert:**

| Event | Trigger |
|-------|---------|
| Runtime switch | `hermes.setup` / `picoclaw.setup` (starting / success / failure) |
| Channel add / refresh | `add_channel`, `channel.refresh_config` (success / failure) |
| Connector set / remove | `connector.set.*`, `connector.remove.*` (success / failure) |
| OAuth refresh | refresh loop — alerted only on ok↔fail state change per provider |
| Skills install | `skills.install` (success / failure) |
| Device soft reset | `device.soft_reset` |
| Claude Code login / WhatsApp pair | terminal pairing outcome (paired / failure / timeout) |
| Default model swap | model sync — only when the version-gated primary/image model actually changes |

Runtime switches are exclusive. While a backend install or switch is running,
another `POST /api/device/agent-runtime` receives `409 Conflict` rather than
starting a competing systemd transition. The web selector stays disabled until
the first switch is confirmed or times out.

HTTP-triggered switches additionally request runtime readiness confirmation for
up to 60 seconds before they stop the old runtime and persist `agent_runtime`;
`systemctl is-active` alone is never treated as proof that a gateway can serve
requests. Each runtime supplies its own probe: OpenClaw runs its authenticated
RPC status probe, Hermes polls authenticated `/health`, and PicoClaw, Codex,
Claude Code, and OpenCode must accept an authenticated WebSocket upgrade.
MQTT runtime setup uses the same probes: it publishes `starting` immediately,
then publishes `success` only after the target probe passes (or `failure` after
the switcher rolls back). The success acknowledgement is emitted before the
required os-server restart so it can reach the broker.

On boot after a runtime switch, the startup sequence may still reconcile
runtime config, channels, and onboarding files; those steps can restart a
gateway. Before it sends the physical wake greeting, os-server therefore
requires the active gateway to remain ready continuously for 15 seconds. This
prevents a greeting from being sent into a gateway that passed an earlier health
probe but is still restarting. The system greeting also tells the agent that its
device skills are available; it should use the relevant skill only for a later
action or device-related request, rather than scanning all skills during boot.

Alerts are enabled whenever `llm_base_url` + `llm_api_key` are set; set
`alerts_disabled: true` in `config/config.json` to mute a device.

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
| POST | `/servo/track` | Start tracking — `{"target":"cup"}` (auto-detect) or `{"bbox":[x,y,w,h]}`. See [vision-tracking.md](../robots/lamp/docs/vision-tracking.md) |
| POST | `/servo/track/stop` | Stop current tracking session |
| GET | `/servo/track` | Get tracking status (active, target, bbox, confidence) |
| POST | `/servo/track/update` | Re-initialize tracker with new bounding box |

### LED (64 WS2812, 8x5 grid)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/led` | LED strip info |
| GET | `/led/color` | Current LED color |
| POST | `/led/solid` | Fill entire strip with one color |
| POST | `/led/paint` | Set individual pixels (array up to 64), or gradient stops with `"gradient": true` |
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

> Wellbeing activity history lives on the OS server HTTP API (port 5000). See `POST /api/wellbeing/log` and `GET /api/agent/wellbeing-history` — entries are JSONL under `/root/local/users/{user}/wellbeing/YYYY-MM-DD.jsonl` with schema `{ts, seq, hour, action, notes}` (action ∈ `drink`/`break`/`sedentary`/`emotional`). HAL no longer hosts wellbeing endpoints.

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

### Piper — on-device TTS

A third TTS provider alongside `openai` and `elevenlabs`, selected as
`tts_provider: "piper"`. Synthesis runs on the device, which removes the two
limits a hosted provider imposes: there is no shared concurrency cap to queue
behind (every unit renders its own audio, so throughput scales with units sold
and costs nothing per utterance), and there is no network round trip, so
time-to-first-audio drops — measured 129–236 ms for short replies against the
2–5 s a hosted call typically takes. The trade is quality: Piper is audibly
behind a hosted neural voice, so it is offered as the free default rather than
as a replacement.

**Nothing ships in the image.** The engine (~26 MB) and each voice (~63 MB) are
downloaded to the device when the operator asks for them in Settings → Voice.
That keeps the image small, means a unit that never leaves the hosted voice
pays nothing, and — because the user's own device fetches from upstream — keeps
Autonomous out of the business of redistributing GPL-3.0 software. Bundling
Piper into the image would reverse that; see `CREDITS.md`.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/voice/piper/status` | Engine installed, voices installed, the download catalogue, and any job in flight. Proxied to HAL and re-wrapped in the standard envelope — the web client rejects a bare payload. |
| POST | `/api/voice/piper/install` | Install the engine. Idempotent: already-installed returns ok, so the UI can call it without checking first. |
| POST | `/api/voice/piper/voice` | Download one catalogue voice. Body `{name}`; names outside the catalogue are refused, so a caller cannot turn this into an arbitrary fetch into `/opt/piper`. |

All three are admin-gated: they install software and write ~63 MB per voice.
HAL serves the same three under `/voice/piper/*`; downloads run on a background
thread and report progress through `job` in the status payload, because a 63 MB
pull is far longer than an HTTP request should be held open for.

Two things the implementation gets wrong if copied carelessly. Piper output
already peaks at full scale, so the `volume_boost` of 2.5 the hosted backends
use would clip every vowel — the backend reports `1.0`. And model load costs
~700 ms, which dominated time-to-first-audio for short replies until the
backend started keeping a pre-spawned process warm and replacing it after each
utterance.

Voices are enumerated from the filesystem (`/opt/piper/voices/*.onnx`), not from
a hardcoded list, so dropping a model in makes it selectable. Which models are
*offered* for download is a licensing decision, recorded with each entry in
`hal/drivers/voice/tts/piper_catalog.py`.


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
   - Seeds `device_type` from the resolved device class (`DEVICE_TYPE` env, else the existing key) so config.json carries it for readers that have no env — HAL's wake words and `software-update`. Provisioning only writes the env, so without this seed the key never exists on a provisioned device. Written once, when the stored value differs
   - Seeds `tts_provider` + `tts_voice` from ROBOT.md `voice:` block when the user hasn't chosen them (persisted once; the user's saved choice always wins; provider absent/unknown → `openai`). When the seeded provider is `elevenlabs` and no voice is declared, picks a language-aware default (`vi`→Ngan, `zh`→Amy, else Rachel)
3. If `SetUpCompleted`:
   - Connect OpenClaw WebSocket
   - Connect MQTT
   - Start ambient behaviors
   - Wait for HAL to answer `GET :5001/health` (up to 120s) before any HAL call. os-server binds :5000 well before HAL's FastAPI is listening, and a first boot also builds the venv and loads models, so an un-gated one-shot call is lost to a connection refused
   - Set speaker volume: the level the user last set (persisted by HAL on every `/audio/volume` change) wins; otherwise the device's `startup_volume` (ROBOT.md front matter, default 100)
4. If not yet set up: wait for `POST /api/device/setup`

## Logging

`HAL_LOG_LEVEL` in the shared `/opt/hal/.env` controls the level for HAL,
OS Server, and bootstrap. Allowed values are `DEBUG`, `INFO` (the default),
`WARN`, and `ERROR`. OS Server writes records at that level and higher to stdout
and the rotating local file `/var/log/os-server.log` (2 MB per file, retaining
the 10 newest backups).

When `GELF_URL` is configured, OS Server ships records at the same configured
level and higher to that central collector through one worker with a bounded queue
of 256 records. Logging never blocks the request path or creates a goroutine per
record: when the collector is slow or unavailable and the queue is full, newly
produced GELF records are dropped (with rate-limited stderr notices) while console
and local rotating-file logging continue. On shutdown, the worker flushes queued
records for up to five seconds before cancelling any remaining delivery.

## Local Intent Matching

When receiving a `voice_command`, `voice_followup`, or `voice` event, the OS server checks local intent first (~50ms):

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
| "volume up" | volume 100 |
| "volume down" | volume 30 |
| "mute speaker" | `POST /speaker/mute` (silent — no TTS confirm) |
| "unmute speaker" | `POST /speaker/unmute` + "Speaker on!" |

Keyword matching is whole-phrase with ASCII word boundaries — "unmute speaker" does not trigger the "mute speaker" rule. The chitchat rules (greeting / farewell / thanks, matched per language) use the same boundary test: a plain substring match let the two-letter phrase "hi" fire inside "this", "his" and "machine", so ordinary sentences like "What is this?" were answered locally with "Hi there!" and never reached the agent.

Chitchat is **off while the realtime voice agent is enabled** — the model receives every voice turn before os-server does and answers social talk itself, in character. Leaving both on meant a canned reply in a different voice barging in on the turns the model happened to stay silent for. Command rules above stay on either way; they genuinely beat a model round-trip. The gate follows `realtime.enabled` live, so toggling it in Settings needs no restart.

No match → forward to OpenClaw.
