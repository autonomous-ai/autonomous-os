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
| POST | `/api/system/reboot` | Admin-gated: acknowledge, then ask HAL to announce and reboot the OS |
| POST | `/api/system/shutdown` | Admin-gated: acknowledge, then ask HAL to announce, release servos, and shut down the OS |

The power endpoints return `202 Accepted` before scheduling their HAL call, so
the browser can receive the acknowledgement before the device becomes
unreachable. Only one reboot or shutdown can be pending at a time; a second
request receives `409 Conflict`. HAL owns the physical sequence: reboot plays
the reboot cue, while shutdown plays its cue and releases servos before issuing
the OS power command.

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
| POST | `/api/agent/speech/cancel` | Physical cancel gesture (single click, called by HAL — loopback-only auth so the button works without a login). Silences every turn currently in flight and stops HAL playback (`StopTTS`, which also clears the pre-synthesised speak-queue). The turns are **not** aborted: they keep running, their tools still fire, and their text still reaches web chat and history — they only lose the speaker. Implemented as a monotone unix-ms watermark (`speechWatermarkMs`): `deliverTTS` drops any reply whose turn was created at or before the mark and logs a `tts_cancelled` flow event. Turn age comes from the runID — device ids end in their creation stamp (`device-chat-7-<unix-ms>`, 13 digits), channel ids (`tg-<messageID>`) have none and fall back to the first time speech was requested for that run. Because new turns are always on the far side of the mark, the user can click and immediately speak again while an older backlog drains silently; the watermark never needs clearing. The same mark also drops the turn's `[HW:]` markers in `fireHWCall` — servos and LEDs stop too, since a device that keeps moving after being told to stop reads as ignoring the user. The run id is put through `resolveRunID` first: the TTS path already holds the device id while HW dispatch may still carry the raw backend UUID for the same turn, and judging them separately muted the reply while the markers fired anyway. `/dm`, `/broadcast` and `/speak` are exempt (the gate sits after them): the click means "stop talking to me" and must not swallow a reply addressed to a Telegram user. The click also calls `FillerManager.CancelAllActive()`. Fillers speak straight to HAL and never pass through `deliverTTS`, so the watermark alone cannot reach them — and because a muted turn keeps running, every tool boundary it crossed re-armed another "one moment" for a reply the user had just cancelled. Every run holding filler state at that instant is on the old side of the mark, so all of them are dropped; the Opening filler for whatever the user says next is armed afterwards and is unaffected. A dropped reply is still posted to HAL's `POST /voice/realtime/history`: the click takes the speaker, not the answer, and the realtime agent's record of what the main agent replied otherwise rides on TTS completion (see `docs/realtime-voice.md`). |
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
| GET | `/servo` | Recordings + animation state + `motion_mode` (`zero` / `hold` / `released`, or `null` when no mode is holding the body) — the posture mode that decides whether `/servo/play` is honoured |
| POST | `/servo/play` | Play animation (idle, curious, nod, headshake, happy_wiggle, sad, excited, shock, shy, scanning, wake_up, music_groove, listening, thinking_deep, laugh, confused, sleepy, greeting, acknowledge, stretching). Idle auto-plays on boot. Answers `{"status":"ignored","reason":"hold"\|"zero"\|"released"\|"sleeping"}` when the mode or the sleep gate drops the play — `"ok"` means the recording actually started. |
| POST | `/servo/move` | Send joint positions with smooth interpolation |
| POST | `/servo/release` | Disable torque on all servos |
| GET | `/servo/position` | Current servo positions |
| GET | `/servo/aim` | List aim directions |
| POST | `/servo/aim` | Aim device head (center, desk, wall, left, right, up, down, user). `left`/`right` change only `base_yaw`; an explicit `center` resets it; every other direction — and the unknown-direction fallback — keeps the current yaw |
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
| POST | `/api/voice/piper/voice/remove` | Delete a downloaded voice and free its ~63 MB. Body `{name}`, catalogue-only for the same reason — an arbitrary name here would delete an arbitrary file. Refuses to remove the last installed voice. |

All four are admin-gated: they install software and write 63–79 MB per voice.
HAL serves the same four under `/voice/piper/*`; downloads run in a background

`piperProxy` **retries a POST while HAL is not answering**, for up to 25 s. Every
voice save restarts HAL (~8 s of downtime, occasionally doubled because two
config paths each request a restart), and a Download or Remove landing in that
window was simply lost — the page said nothing changed and the operator had to
guess when to try again. Only a **failed dial** is retried, and the distinction
carries the whole safety argument: a dial that never connected proves the
request was not delivered, so replaying it cannot repeat an effect. A timeout
proves nothing of the sort — the deadline covers reading the reply, so HAL may
have done the work and answered slowly — and those surface as a plain failure.
Any reply, including a refusal, is final and passed straight through. GET is
deliberately
excluded — the status poll's failure is what tells the page the device is
restarting, and holding those open would stack requests and hide the state.
Covered by `piper_test.go`, which restarts a listener under the call.

**A download does not run inside HAL.** `hal/routes/piper_download.py` is
launched by `systemd-run` as a transient unit, and the two sides agree through
a job file at `/var/lib/autonomous/piper-job.json` instead of shared memory.
This is not over-engineering: saving *any* voice setting makes os-server run
`systemctl restart hal` (`device/config_update.go`), and hal.service is
`KillMode=control-group`, so an in-process thread — or any ordinary child — was
killed mid-transfer. The record of the job died with it, so the page reverted
to `Download 63 MB` as though the click had never happened, with no error and
nothing to retry from. The worker imports nothing from `hal`: the package pulls
in hardware drivers on import, which a downloader has no business touching, and
staying dependency-free means it keeps running even when HAL will not start.

Each run gets its **own unit name** (`autonomous-piper-download-<ns>`). A fixed
name collides with the run before it: a finished unit sits in `inactive` for a
moment before `--collect` reaps it, and `systemd-run` refuses a name that still
exists. That failure fell through to the in-process fallback, which then died
with the next HAL restart and surfaced as *download stopped unexpectedly* for no
visible reason. The fallback now logs systemd's own stderr, because falling back
silently is how a download ends up inside HAL's control group unnoticed.

Nothing restarts HAL for a download. Voices are listed from the filesystem per
request and the model path is resolved per utterance, so a voice is listable and
speakable the moment its file lands — measured: downloaded at 18:32:29 on a HAL
that started at 18:31:59, listed and spoken at 18:33:11 with no restart between.
Applying a voice does not restart HAL either. `POST /voice/tts/config` sets
provider, voice, key and base URL on the running TTS service, which reads all of
them per utterance, so the change takes effect on the next sentence.

The phrases the device says about itself — restart, shutdown, reboot, sleep —
are **rendered into the TTS cache ahead of time**, at boot and again whenever
`/voice/tts/config` changes provider or voice (the cache key includes both, so a
voice change invalidates every clip). They play at the worst possible moments:
the restart notice is spoken while HAL is tearing down, the boot cue while every
other service is still coming up. On Piper a cache miss there means loading a
63 MB model on a saturated CPU — measured on an 8-core sun60iw2, the load alone
is 2–3.4 s and the restart phrase synthesises at 1.1x realtime, close enough to
breaking even that a little extra load starves the audio stream and the speech
comes out slurred. A hit costs no synthesis at all.

The realtime flag compares before and after rather than reacting to presence.
The settings page puts a `realtime` block in *every* save, so treating it as a
change restarted HAL on every save — which would have made the live TTS push
above dead code.

### Autonomous defaults

A shipped device carries the Autonomous team's proxy credentials in
`llm_api_key`, `llm_model` and `llm_base_url`, and every other section starts
from those same three values. Typing a personal key over them used to destroy
them outright — devices reached the field with no way back to the credentials
they were sold with.

`autonomous_defaults` is a top-level object in `config.json` holding
`base_url` / `api_key` / `model`. It is written **once**, by
`captureAutonomousDefaults`, immediately before the first save that carries any
credential — LLM, TTS, STT, or realtime key/URL — and never written again.
Capturing twice would store the operator's own key under the Autonomous name
and lose the real one for good, which is the exact failure it exists to prevent.
A save touching nothing credential-shaped (wifi, rename, channels) does not
trigger it, and a config with no credentials to preserve is skipped so an empty
set is never mistaken for a valid default. Only a factory reset clears it.

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/device/restore-defaults` | Put one section back on the shipped credentials. Body `{"section": "llm" \| "voice" \| "realtime"}`. Admin-gated. |

Restore is **per section**, because that is how an operator thinks about it —
they swapped the brain, or the voice provider, and want that one thing back.
Each section takes the slice of the stored set it started from: the AI Brain
url + key + model, realtime and the voice pipeline url + key. Qwen realtime is
refused: it talks straight to the Alibaba host with its own credentials, and the
shipped set would produce a 401 there.

It is implemented as an ordinary `UpdateConfig` rather than a direct write, so
it inherits every side effect a manual edit gets — hal restart or the live TTS
push, gateway model sync, agent session reset. A hand-rolled save would drift
from that list the first time someone adds to it.

`has_autonomous_defaults` on `GET /api/device/config` says whether anything is
stored, never the values. The web uses it to decide whether to offer the action
at all.

HAL reads **each service's own credentials**, falling back to the AI Brain's
when they are blank: `tts_api_key`/`tts_base_url` for TTS, `stt_api_key`/
`stt_base_url` for STT, `llm_api_key`/`llm_base_url` otherwise. On most devices
all three are the same string, because the settings page mirrors the brain's
key and URL into the other two while those are blank. It matters when the brain
points elsewhere: a device with `llm_base_url` on openrouter and `tts_base_url`
on the autonomous proxy was building
`openrouter.ai/api/v1/elevenlabs/text-to-speech/…` and taking a 404 on every
spoken reply, because the ElevenLabs backend appends `/elevenlabs` to whatever
base it is handed and it was being handed the brain's. The config had the right
URL all along; nothing read it.

`device/config_update.go` splits what used to be one `voiceSnapshot` in two:
`bootSnapshot` (LLM and STT keys and URLs — genuinely read at import, still
worth a restart) and `ttsSnapshot` (provider, voice, TTS key and URL — pushed
live). A voice change is the most common save an operator makes, and restarting
for it took the microphone, speaker and wake word down for ten to fifteen
seconds; any admin click landing in that window was lost, because HAL was not
listening. If the live push fails, os-server falls back to the restart — a voice
that was saved but never reached HAL is worse than the restart it avoided.

A job is **claimed before the POST replies**, and the reply carries it. Leaving
the claim to the worker loses a race the UI cannot recover from: the panel only
polls while a job is active, so if its first read lands before the worker's
first write it concludes nothing started and stops looking, and a
several-minute download runs to completion invisibly. Claiming under the same
lock that checks for a running job also makes a double-click one download.

The reader treats an active job as real only while its pid exists, so a worker
killed by anything other than its own error handler shows as stopped rather
than as a download frozen forever. The start-up orphan sweep skips the files a
running job owns — transfers now outlive HAL, so the sweep runs *during* one,
and deleting its `.part` would break the exact case this design protects.

The job reports `bytes_done`/`bytes_total` alongside `percent`, tracked for the
model only — the sidecar is a few KB and would flicker the counter to a tiny
total and back. A failed voice install deletes its own partial files, and HAL
sweeps orphaned sidecars and `.part` files once at start: the listing keys off
`.onnx`, so a sidecar whose model never arrived is invisible in the UI while
still occupying space on a small card.

Removal enforces one invariant: **never delete the last model.** HAL is not told
which voice is configured — os-server sends it with each `/voice/speak` call —
so it cannot refuse "the one in use", and it does not try. Removing any other
voice is survivable because an unknown voice falls back to one that is
installed; removing the last one is not, because the backend then has nothing
to load and the device goes silent. The UI additionally hides Remove on the
in-use row, so switching comes before deleting.
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

The backend reports itself available when the binary and **any** voice are
present, not the configured one specifically. A device can legitimately be set
to a voice it does not yet have — the operator saves the choice while the 63 MB
model is still downloading — and gating on the exact name would take TTS
offline entirely. Instead an unknown voice falls back to the default, then to
whatever is installed, and logs the substitution once per name. Speaking in the
wrong voice is a fault that explains itself; a silent device reads as broken
hardware.

`GET /api/device/voices?provider=piper` **fails rather than answers empty** when
HAL is unreachable. Voices are files under `/opt/piper`, so HAL is the only
thing that can know what is installed; an empty success would be a claim
os-server cannot make, and the web takes the reply as authoritative — the picker
empties, and since it only refetches on a provider or language change, it never
fills back in. Every voice save restarts HAL, so that window is hit routinely.
An error leaves the client holding its last known-good list.

For the same reason `domain.TTSVoicesByProvider` is **empty** for Piper: no image
ships a voice, so any name offered as a fallback would be a name the device does
not have — and the web UI would save it as the configured voice.


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

## Off-device run (laptop)

`make os-dev` runs the **same binary** that ships to the board — no build tag,
no second code path. Only the device-absolute paths move, through the env vars
`system/lib/syspath` reads. **Unset env = board defaults, byte for byte**
(`runtimes/codex/paths_default_test.go` asserts this).

| Env var | Default (device) | Used for |
|---------|------------------|----------|
| `CODEX_HOME` | `/root/.codex` | Codex state dir — config.toml, auth.json, `.env`, `skills/`, `sessions/`, `workspace/`. Anchors every codex path on both the client and `codex-gatewayd` |
| `CODEX_PORT` | `18792` | Bridge WebSocket port (`WSURL` and the gatewayd listener) |
| `CODEX_WS_TOKEN` | `autonomous_codex_token` | Bearer token os-server sends to the bridge |
| `OS_AGENT_HOME` | `/root` | Root a Telegram coding session resolves `~` and relative folders against |
| `OS_AGENT_STATE_PATH` | `/root/config/agent_state.json` | Runtime-switch history (persona migration) |
| `OS_BOOTSTRAP_CONFIG` | `/root/config/bootstrap.json` | The file os-server reads `metadata_url` from — the base for skill zips and the skill watcher |
| `OS_LOG_FILE` | `/var/log/os-server.log` | Rotating log file |
| `DEVICE_TYPE` / `DEVICES_DIR` | — / `/opt/devices` | Body selector and `robots/<type>/` root (pre-existing) |

`config.json` needs no env: `configPath` is `config/config.json` relative to the
cwd, so `os-dev` runs from the state dir exactly as systemd's
`WorkingDirectory=/root` does on the board.

A full laptop stack is three terminals:

```bash
make sim          # HAL on :5001
make codex-dev    # codex bridge on $CODEX_PORT
make os-dev       # API on :5000
make web-dev      # web UI on :5173 (optional)
```

os-server serves no HTML: on a board nginx serves `web/dist` and proxies `/api`
and `/hw` to it. `make web-dev` puts Vite in nginx's place, with `LAMP_PROXY`
(default `http://127.0.0.1:5000`) naming the device the SPA talks to — a `.env`
in `web/` still wins, so pointing at a real Pi is unchanged. Open
**`http://localhost:5173/monitor`**; Vite binds `[::1]` only, so `127.0.0.1:5173`
is refused. Admin routes need auth — log in with the device password, or append
`?llm_api_key=<the key in config.json>` once and the SPA exchanges it for a
session cookie and scrubs it from the address bar.

Three of the six log tabs work off-device. `hal` and `os-server` follow
`OS_HAL_LOG_FILE` / `OS_LOG_FILE`, and the Agent tabs follow
`OS_AGENT_BRIDGE_LOG` — `make codex-dev` tees the bridge to a file because a
laptop has no journal to read. `bootstrap` (the worker is not run off-device)
and `buddy` (a Mac app with no log here) stay empty by design; unset env leaves
all six exactly as they resolve on a board.

Makefile knobs: `OS_STATE_DIR` (default `/tmp/autonomous-os`), `OS_AGENT_RUNTIME`
(default `codex`), `CODEX_HOME` (default `$HOME/.codex`), `CODEX_PORT`,
`CODEX_BIN`. `scripts/dev/os-dev-seed.sh` writes `device_type`, `agent_runtime`
and `set_up_completed: true` into the state dir's config.json — the last one
matters because the startup sequence that runs presync and `EnsureOnboarding`
is gated on it (`server/config_watch.go`), so without it the workspace stays
empty. Nothing in the target installs the codex CLI itself — that is expected to
be on `PATH` already.

Skills DO install themselves. `os-dev-seed.sh` also seeds a `bootstrap.json`
carrying `metadata_url`, derived from the same `GCS_BUCKET` / `BUCKET_PREFIX`
that `scripts/release/ota-config.sh` defines, so the dev URL cannot drift from
what `upload-skills.sh` publishes. With it set, `EnsureOnboarding` runs the same
`downloadSkills()` the board runs: every skill this `DEVICE_TYPE` supports is
pulled as `<base>/skills/<name>.zip` into `$CODEX_HOME/skills`, and the skill
watcher then refreshes it on version changes. The CDN objects are public, so no
credentials are involved. Seeded once — an edited `bootstrap.json` survives.

`metadata_url` is the ONLY key os-server reads from that file, and its only
consumers are the skill watcher and the runtimes' `otaBaseURL()` helpers, so
setting it off-device enables skills and nothing else — OTA self-update lives in
the separate `bootstrap-server` binary, which `make os-dev` does not run.

### Full media + voice on the laptop

`make sim` alone boots HAL with virtual devices. `make sim SIM_MEDIA=host` opens
the Mac's microphone, speaker and camera **and** runs the real voice pipeline
(STT → realtime → `[turn] route=…` dispatch → this server), so a spoken turn
travels the same path it does on a board. The `sim` target sets three paths for
it:

| Env | Points at | Why |
|-----|-----------|-----|
| `OS_CONFIG_PATH` | `$OS_STATE_DIR/config/config.json` | The one file HAL and os-server share, as `/root/config/config.json` is on a board. Carries the credentials **and** `agent_runtime` |
| `HAL_SNAPSHOT_DIR` | `$CODEX_HOME/media/hal-snapshots` | Where `?save=true` writes. Must sit under the runtime's own home or the agent cannot read the frame and `GET /api/sensing/agent-snapshot/…` cannot serve it |
| `HAL_SNAPSHOT_PERSIST_DIR` | `$SIM_STATE_DIR/snapshots` | `/var/lib/hal/snapshots` is root-only |
| `HAL_TTS_CACHE_DIR`, `HAL_CALIBRATION_DIR`, `HAL_USER_BEARING_PATH`, `HAL_FACE_HEIGHT_PATH`, `HAL_VOICE_STRANGERS_DIR`, `HAL_DL_STALL_LOG` | `$SIM_STATE_DIR/…` | The rest of HAL's writable state, rooted at `/var/lib/hal` or `/root/local` on a board |
| `HAL_CODEX_WORKSPACE_DIR` | `$CODEX_HOME/workspace` | The realtime agent's `memory.jsonl` is derived from it |

These fail far from their cause, which is why they are set as a block rather
than one at a time: the TTS cache one surfaced as `POST /voice/speak 409` with
the real `PermissionError: /var/lib/hal` buried in a background thread's
traceback. Two remaining defaults are read-only model paths
(`/root/local/models`, `/opt/piper`) — absent on a laptop, the feature that
needs them simply stays off. `POST /audio/volume` answering 503 is also
expected: macOS has no ALSA mixer.

Put the credentials in that config.json (Settings in the web UI writes the same
file). `llm_api_key` + `llm_base_url` alone cover LLM, `AutonomousSTT`, TTS,
image description and Gemini Live — the realtime key falls back to `llm_api_key`
and its endpoint to `llm_base_url` + `/ws/gemini` (`hal/config.py`), so no
separate Google credential is involved. `deepgram_api_key` is optional.

Copying a real device's config.json is the fastest way to a full-option laptop,
but blank two keys first: `telegram_bot_token` (one bot cannot have two pollers —
the laptop would steal the device's messages) and `mqtt_endpoint` (the laptop
would subscribe the device's own topics). Neither is an AI capability, so
nothing above is lost.

Servo has no physical body here: `http://127.0.0.1:5001/simulator` is the
readout, driving the same `/servo/*` and `/led/*` endpoints a skill calls.

Two things to know on macOS:

- Microphone and Camera access must be granted to the terminal app running HAL
  (System Settings > Privacy & Security). Enumeration is not permission — the
  device list is populated either way and only the first real read fails — so
  HAL probes both at boot and falls back to the virtual device with a logged
  `[sim-media]` reason rather than failing a turn later.
- AirPlay Receiver also listens on `*:5000`. os-server binds `127.0.0.1:5000`,
  but a request to `localhost:5000` can still land on AirTunes — turn the
  receiver off (System Settings > General > AirDrop & Handoff) or change
  `httpPort`.
- `presync.sh` regenerates `config.toml` on every boot and keeps only
  `[mcp_servers.*]`. `os-dev-seed.sh` copies a pre-existing one to
  `config.toml.pre-os-dev` once, so pointing `CODEX_HOME` at a real install is
  not a one-way door.

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
