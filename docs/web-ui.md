# Web UI — Monitor Dashboard

## Last updated: 2026-05-27

---

## 1. Overview

The device's Web UI is a React SPA (Single Page Application) built with **React 19 + TypeScript + Vite + Tailwind CSS 4**, serving two purposes:

1. **Setup flow** — WiFi, LLM provider, messaging channel onboarding (`/setup/*` pages)
2. **Monitor Dashboard** — Real-time device status monitoring (`/monitor`)

Build output (`dist/`) is served by nginx at root `/` on the device.

### 1.1 Browser Tab Title

The browser tab title (`document.title`) reflects the focused page/tab so multiple device tabs are distinguishable. Driven by the shared `useDocumentTitle` hook (`os/services/web/src/hooks/useDocumentTitle.ts`); format is `Lamp · <segment>[· <sub-segment>]`.

| Route / state | Title |
|---------------|-------|
| `/setup` (and `/` when not provisioned) | `Lamp · Setup` |
| `/monitor#<section>` (active section) | `Lamp · <section label>` — e.g. `Lamp · Chat`, `Lamp · Overview`, `Lamp · Info`, `Lamp · Flow`, `Lamp · Users`, `Lamp · Camera`, `Lamp · Sensing`, `Lamp · Analytics`, `Lamp · Servo`, `Lamp · Logs`, `Lamp · CLI` |
| `/setting#<section>` (Settings, active section) | `Lamp · Settings · <section label>` — e.g. `Lamp · Settings · General`, `Lamp · Settings · Wi-Fi`, `Lamp · Settings · AI Brain`, `Lamp · Settings · Language`, `Lamp · Settings · Voice`, `Lamp · Settings · My Voice`, `Lamp · Settings · Face`, `Lamp · Settings · Channels`, `Lamp · Settings · MQTT` |
| `/gw-config` | `Lamp · GW Config` |

The static `<title>Lamp Setup</title>` in `index.html` is the pre-mount fallback; the hook overrides it once React mounts and reverts to the previous title on unmount.

---

## 2. Directory Structure

```
os/services/web/
├── src/
│   ├── pages/
│   │   ├── Monitor.tsx        # Dashboard monitor (main file)
│   │   └── ...                # Setup pages
│   ├── components/
│   │   └── ui/                # shadcn/ui components
│   ├── index.css              # Global styles + theme variables
│   └── main.tsx
├── vite.config.ts
└── package.json
```

---

## 3. Monitor Dashboard (`/monitor`)

### 3.1 Overall Design

Monitor uses a dedicated dark theme with class `.lm-root` (defined in `index.css`), **not using Tailwind** — all styling uses inline styles with CSS variables `--lm-*`.

Layout: **Fixed 192px sidebar + flexible main area**, 100vh height.

### 3.2 Sidebar Navigation

4 sections toggled via local state (`section: Section`):

| Icon | Section | Content |
|------|---------|---------|
| ◈ | Overview | Full system overview |
| ⬡ | System | CPU/RAM/Temp details + history |
| ◎ | Workflow | OpenClaw event feed real-time |
| ⬟ | Camera | MJPEG stream + Display LCD |

Bottom of sidebar shows OpenClaw status (online/offline) and last update time.

### 3.3 Dark Theme Variables

Defined at `.lm-root` in `index.css`:

```css
--lm-bg:          #0C0B09   /* Main background */
--lm-sidebar:     #111009   /* Sidebar */
--lm-card:        #17160F   /* Card background */
--lm-surface:     #1E1D14   /* Surface inside card */
--lm-border:      #2A2820   /* Border */
--lm-border-hi:   #3A3828   /* Border highlight */
--lm-amber:       #F59E0B   /* Primary color (warm lamp) */
--lm-amber-dim:   rgba(245,158,11,0.12)
--lm-amber-glow:  rgba(245,158,11,0.35)
--lm-teal:        #2DD4BF
--lm-green:       #34D399
--lm-red:         #F87171
--lm-blue:        #60A5FA
--lm-purple:      #A78BFA
--lm-text:        #F0EEE8
--lm-text-dim:    #9A9080
--lm-text-muted:  #504A3C
```

### 3.4 Settings (`/setting`) — shared shell

Settings is **not a separate page**. It is an area of the same Monitor shell (`os/services/web/src/pages/monitor/index.tsx`), reached at the `/setting` route. In `App.tsx`, `/monitor` and `/setting` are child routes of a single layout route whose element renders `<Monitor/>`; React Router keeps that element mounted while only the matched child path changes, so the sidebar does **not** remount when switching between Monitor and Settings (no full-page flash). The shell derives its area — `"monitor"` or `"setting"` — from `useLocation().pathname`.

The Settings collapsible group lives in the shared sidebar `NAV` (`os/services/web/src/pages/monitor/types.ts`). Clicking a Settings leaf navigates to `/setting` and renders `SettingsPanel` (`os/services/web/src/components/edit/SettingsPanel.tsx`) in the main area; clicking a Monitor leaf navigates to `/monitor`.

**URL hash scheme** — the in-memory section keeps internal `settings:*` ids, but the URL hash uses short labels in the setting area (helpers `sectionToHash`/`hashToSection` in `types.ts`):

| Leaf | URL |
|------|-----|
| General | `/setting#general` (internal `settings:device`) |
| Wi-Fi | `/setting#wifi` |
| My Voice | `/setting#voice` |
| Face | `/setting#face` |
| AI Brain | `/setting#llm` |
| Language | `/setting#stt` |
| Voice | `/setting#tts` |
| Channels | `/setting#channel` |
| MQTT | `/setting#mqtt` |

Monitor leaves serialize as the plain id, e.g. `/monitor#overview`, `/monitor#system`, `/monitor#flow`. Defaults: `/monitor` with no/invalid hash → `overview`; `/setting` with no/invalid hash → `general` (URL normalized to `/setting#general`). Deep-links (e.g. `/setting#wifi`) and browser back/forward are honored via a `useLocation`-driven effect. Non-debug users only see the leaves in `PUBLIC_SECTIONS` (which includes Chat, Overview, Info, Flow, Camera, Users, Bluetooth, **Logs**, **CLI**, and the Settings leaves); `?debug=true` reveals the rest (Sensing, Analytics, Servo, API Docs, Agent gateway).

The standalone `/edit` page (`EditConfig.tsx`) still exists as a fallback and embeds the same `SettingsPanel`.

---

## 4. Polling & Data Sources

Monitor polls system/HW APIs every **3 seconds**. Flow uses file-backed hybrid mode: REST seed + live stream.

### 4.1 OS Server (Go, port 5000, prefix `/api`)

| Endpoint | Data |
|----------|------|
| `GET /api/system/info` | CPU load, RAM (KB), temperature, uptime, goroutines, version, deviceId, capabilities (declared capability names — both the Monitor and the Edit/Settings page gate hardware tabs on these; see the shared `useCapabilities` hook) |
| `GET /api/system/network` | SSID, IP, public IP, Tailscale IP, signal (dBm), internet (bool) |
| `GET /api/openclaw/status` | name, connected (bool), sessionKey (bool), version, emotion, uptime (OS server WS uptime, secs), agentUptime (OpenClaw process uptime from hello-ok `server.uptimeMs`, secs — survives OS server restarts) |
| `GET /api/openclaw/recent` | Latest flow events from today's JSONL file (`local/flow_events_<date>.jsonl`) |
| `GET /api/openclaw/flow-events?date=YYYY-MM-DD&last=500` | File-backed flow events API used for Flow seed/history |
| `GET /api/openclaw/flow-stream` | File-backed live stream (SSE) for Flow updates when JSONL changes |
| `GET /api/openclaw/events` | Monitor bus SSE endpoint (kept for compatibility) |
| `POST /api/system/force-update` | Triggers OTA check via bootstrap worker (proxies to `localhost:8080/force-check`) |

> **Note on format**: The OS server API returns `{ status: 1, data: <payload>, message: null }` on success.

### 4.2 HAL (Python/FastAPI, port 5001, prefix `/hw`)

| Endpoint | Data |
|----------|------|
| `GET /hw/health` | Status of 8 hardware: servo, led, camera, audio, sensing, voice, tts, display |
| `GET /hw/presence` | state, enabled, seconds_since_motion |
| `GET /hw/voice/status` | voice_available, voice_listening, tts_available, tts_speaking |
| `GET /hw/servo` | available_recordings, current, bus_connected, robot_connected |
| `POST /hw/servo/upload` | Upload a new servo recording CSV (`timestamp` + `<joint>.pos` columns) |
| `GET /hw/display` | mode, hardware, available_expressions |
| `GET /hw/audio/volume` | control, volume (0-100) |
| `GET /hw/led/color` | led_count, color [R,G,B], hex (#rrggbb) |

---

## 5. Section Details

### 5.1 Overview Section

Cards included:

**OpenClaw AI**
- Connected/disconnected status
- Agent name
- Session key: Acquired / Pending

**Network**
- SSID + Signal bars (4 levels based on dBm)
- IP address
- Tailscale IP (only shown when `tailscale ip -4` returns an address — works
  in both kernel and userspace-networking modes)
- Internet status

> The Setup gate (`App.tsx`) auto-redirects from AP/non-LAN hostnames to the
> device's LAN IP, but skips this redirect when the hostname falls in the
> Tailscale CGNAT range `100.64.0.0/10` — visiting via Tailscale is treated
> as a deliberate remote-access path.

**Presence**
- State (active/idle)
- Sensing enabled/disabled
- Time since last motion detection

**Voice & TTS**
- Mic available + listening (LIVE badge)
- TTS available + speaking (SPEAKING badge)
- Current volume

**Hardware** (horizontal card)
- 8 badges: Servo / LED / Camera / Audio / Sensing / Voice / TTS / Display
- **LED color swatch**: rounded square showing current LED strip color with hex code. Fetched from `GET /hw/led/color`.

**Scene** (lighting presets)
- Shows available scene presets (reading, focus, relax, movie, night, energize). Fetched from `GET /hw/scene`.
- Clickable buttons activate a scene via `POST /hw/scene` with `{"scene": "<name>"}`.
- Active scene highlighted with amber accent.

**Servo Pose**
- Currently running pose (current)
- List of available servo recordings/animations (from `GET /hw/servo`)
- Each can be played via `POST /hw/servo/play` (recording name)
- UI also provides an `Upload CSV` button to add/replace recordings via `POST /hw/servo/upload` (multipart: `file`, `recording_name`)

**Display Eyes**
- Currently displayed expression (mode)
- List of available expressions

> **Capability-gated cards.** Overview hardware cards are hidden on devices that
> don't have the underlying capability, so the page only shows what the device can
> actually do (e.g. intern-v2 has no servo, scene, or expression):
> - **Emotion** and **Servo Pose** gate on the declared capability (`expression` / `motion`) from `GET /api/system/info` → `capabilities`.
> - **Scene** is a route *within* the `light` capability (lamp declares `light:[led,scene]`; intern-v2 declares `light:[led]`), so it can't be told apart by the capability list — it renders only once `GET /hw/scene` returns scenes.

**System quick stats**
- CPU, RAM, Temp, Uptime as pills

### Sidebar Footer

Below the nav items and OpenClaw status, the sidebar shows versions for all three repos:
- **Web** (teal): injected at build time from `package.json` via Vite `define` (`__WEB_VERSION__`)
- **OS server** (amber): from `GET /api/system/info` → `version` field (Go ldflags)
- **HAL** (blue): from `GET /api/system/info` → `halVersion` field. The OS server calls HAL `:5001/version` on the loopback once per minute (cached) and re-exposes it through the OS server API, so the browser doesn't need direct access to `/hw/*` (nginx gates `/hw/` to loopback only).
- **Force Update** button: triggers `POST /api/system/force-update` → bootstrap OTA check. Shows "Checking…" while busy, then "Triggered"/"Failed" feedback for 3 seconds.

### 5.2 System Section

**Performance** — 3 GaugeRing SVGs:
- CPU: amber color, shows `%`
- Memory: blue color, detail `used/total MB` (converted from KB: `value / 1024`)
- Temp: teal (< 70C) or red (>= 70C), scale 0-85C

**CPU History / RAM History** — Sparkline chart (area + line):
- Stores 60 history points (`HISTORY_LEN = 60`)
- Updates every 3 seconds

**Process**: goroutines, uptime, version, deviceId
**Network Detail**: SSID, IP, signal, internet

### 5.3 Workflow Section

File-backed hybrid feed:

| Type | Color | Meaning |
|------|-------|---------|
| `lifecycle` | amber | Agent starts / ends run |
| `tool_call` | teal | AI calls a tool |
| `thinking` | purple | AI is thinking (streaming) |
| `assistant_delta` | blue | AI is responding (streaming delta) |
| `chat_response` | green | Final chat response |

Each event displays: type badge, phase (if any), runId (first 8 chars), timestamp, summary text, error (if any).

- Initial/history load via `GET /api/openclaw/flow-events`.
- Live updates via `GET /api/openclaw/flow-stream` (SSE emitted on file change).
- Fallback polling (2s) is used only if live stream disconnects.
- Displayed turns/events are fully derived from JSONL flow logs.

**Turn Pipeline (SVG)** — Implemented by `FlowDiagram` in `os/services/web/src/pages/Monitor.tsx`. Full layout (three clusters: OS server / HAL / OpenClaw, column grid, Cron vs OpenClaw, HAL row aligned with Tool, approximate coordinates) is documented in **`docs/flow-monitor.md`**; Vietnamese summary in **`docs/vi/flow-monitor_vi.md`**.

Turn Pipeline grouping behavior:
- Turns are still started by input/trigger events (`sensing_input`, `chat_input`, `schedule_trigger`, etc.).
- The UI now anchors each turn to the first detected `run_id` (from event root or detail payload).
- For user mic actions: each `sensing_input` with `[voice]` / `[voice_command]` (and `voice_pipeline_start`) creates a separate turn even if events share the same `run_id`.
- For web monitor chat: each `sensing_input` with `[web_chat]` creates its own boundary turn (icon 🖥, filter category **Web**) so it isn't merged with adjacent voice/sensing turns.
- For user chat actions: each `chat_input` (telegram input) creates its own boundary turn, so it won't be merged with adjacent voice turns even if OpenClaw reuses the same `run_id`.
- If a later event has a different `run_id`, Monitor splits it into a new inferred agent turn.
- **Turn type badge** (`motion`, `voice`, …): merged segments that share one `run_id` may include both camera motion and a voice line; the first segment used to win, so the badge could read `motion` while the utterance was voice. After grouping, if any `sensing_input` in the turn is `[voice]` or `[voice_command]`, the badge uses that (voice beats motion for the same run).
- `OUT` text is only taken from `tts_send`/`intent_match` events matching the turn `run_id` (or events without run_id), preventing cross-turn input/output mismatch.
- LLM token usage is shown on LLM nodes (Agent Call / Thinking / Response): `in/out` and, when available from `token_usage`, `cache read/write` + `total`.
- For Telegram input, placeholder summaries like `[telegram]` no longer lock the `IN` field; when a later event with the same `run_id` contains real message text, the UI replaces the placeholder with that text (and will override earlier `sensing_input` text like SOUND within the same UI turn). If the Telegram input message is completely missing (ghost turn), the turn type becomes `unknown` to avoid misleading “TG IN”.
- Temporary fallback: when Telegram text is unavailable, UI displays `Message content from telegram`.
- Turn badges always render the `IN` row; if input is missing, UI shows `Input not captured`.
- Flow Panel header actions include **`↓ Bundle`**, **`full day`**, **`🗑 Log`**.
- **`↓ Bundle`** — one click saves **two files**: (1) server JSONL tail via `fetch` + blob (`GET /api/openclaw/flow-logs?last=500`), (2) UI snapshot JSON (`events` + `groupIntoTurns` → `lamp_flow_ui_snapshot_*.json`).
- **`full day`** — `GET /api/openclaw/flow-logs` without `last` (whole day JSONL).
- `🗑 Log` asks for confirmation and calls `DELETE /api/openclaw/flow-logs` to truncate the server flow log, then clears current Flow UI events.
- Turn history list shows **all turns** for the day (newest first), derived from the **last 10 000** flow events — covers a full day of typical activity.
- Flow event memory is capped at 10 000 events.
- Telegram stitching heuristic: if a Telegram fallback input turn (without real input text) is immediately followed by an agent-output turn within 30s, Monitor stitches them into one turn so the reply stays with the original Telegram input.

### 5.4 Camera Section

- **Camera Stream**: MJPEG live stream from `GET /hw/camera/stream` (downscaled + throttled; default ~10fps, ~320px width)
- **Display Eyes (GC9A01)**: Round 1.28" screen snapshot from `GET /hw/display/snapshot`, displayed as circle with amber glow. Has Refresh button.
- **Camera Snapshot**: Static image from `GET /hw/camera/snapshot`, with Capture button to take new shot.

### 5.5 Logs Section

- Dedicated runtime log panels for HAL, OS (os-server), and OpenClaw service logs.
- Each panel streams via SSE (`GET /api/logs/stream?source=<source>`) with fallback polling.
- Supports level filtering (ALL/DEBUG/INFO/WARN/ERROR) and text/regex search.

> **Note**: Camera serves a dual role — (1) live stream display for user viewing, (2) automatic sensing data source. Sensing service reads a frame from camera every 2s to detect motion, faces (Haar cascade), and light level. When significant events are detected (person appears, large motion), a full-resolution JPEG auto-snapshot is sent with the event to OpenClaw AI for vision analysis.

### 5.6 Chat Section

Interactive chat interface for communicating with the agent. Layout: sidebar (conversation list) + main chat area.

**Conversations**
- Multiple conversations stored in localStorage (max 50, 200 messages each)
- Sidebar with search, pin, rename (double-click), delete (double-click confirm), export as TXT
- Grouped by date: Today / Yesterday / This week / Older, pinned at top
- Keyboard shortcut: Cmd/Ctrl+N for new chat
- Collapsible sidebar

**Message Input**
- Textarea with Shift+Enter for multi-line, Enter to send
- File/image attachment (max 10 MB): button, drag-drop, clipboard paste
- Messages sent via `POST /api/sensing/event` with `type: "web_chat"`. The handler tags the run via `MarkWebChatRun(runID)` so the agent reply is suppressed at TTS (rendered in this UI only) and skips the physical wake greeting / opening filler. Web chat with image attachment is saved to `/tmp/web-chat-*.jpg` and surfaced to the agent via `[image: <path>]`.

**Real-time Streaming**
- **Thinking indicator**: collapsible purple block showing LLM reasoning tokens as they stream in (`thinking` events). Click to expand full text (max-height 200px scrollable). Auto-hides on response completion.
- **Assistant delta streaming**: response text appears token-by-token via `assistant_delta` events, instead of waiting for final response. Fallback to `chat_response` partial events for non-agent paths.
- **Tool call chips**: teal badges showing tools the agent invoked during the response (emotion, LED, servo, audio, etc.). Displayed above the message bubble during streaming and persisted on completed messages.

**Response Handling**
- Tracks response by `runId` correlation across SSE events
- Inline HW control markers (`[HW:/emotion:...]`) stripped from displayed text
- 30-second timeout: if streaming text received, shows partial text; otherwise shows error with retry button
- Local intent fast path: sub-50ms responses bypassing agent
- Busy/dropped handling: shows "busy — try again"
- Markdown rendering: bold, italic, inline code, code blocks, URLs, ordered/unordered lists

**Data Flow**
```
Chat UI → POST /api/sensing/event → SensingHandler
  → openclaw.SendChatMessage() → WebSocket chat.send → OpenClaw
  → Response streams via WebSocket (thinking → assistant deltas → lifecycle end)
  → SSE /api/openclaw/flow-stream → Chat UI updates message in real-time
```

---

## 6. LED Color API

### Problem
Original `GET /hw/led` only returned `{ led_count: 64 }` — no current color info.

### Solution
Added `GET /hw/led/color` to `os/hal/server.py`:

```python
@app.get("/led/color", response_model=LEDColorResponse, tags=["LED"])
def get_led_color():
    """Get the current LED color (last color set on the strip)."""
```

**Color priority:**
1. `sensing_service.presence._last_color` — base color tracked when AI sets it
2. Fallback: `rgb_service.strip.getPixelColor(0)` — read directly from hardware

**Tracking added for:**
- `POST /led/solid` (existing)
- `POST /scene` (existing)
- `POST /emotion` (added — this is the path AI uses most)

> **Note**: `GET /hw/led/color` is **read-only**, monitor only reads, does not set color.

---

## 7. Reusable Components (internal to Monitor.tsx)

| Component | Description |
|-----------|-------------|
| `GaugeRing` | SVG ring chart with drop-shadow glow, 0.7s transition |
| `Sparkline` | SVG area + line chart, accepts number array |
| `HWBadge` | Green/red badge for hardware status |
| `StatusDot` | Green/red dot with glow |
| `SignalBars` | 4-bar WiFi signal (thresholds: -50/-65/-75/-85 dBm) |
| `StatPill` | Row label + value in card |

---

## 8. Global Source Footer (GPL v3 §6 Compliance)

`os/services/web/src/components/SourceFooter.tsx` is a tiny `position: fixed` link mounted at the App root (`App.tsx`, outside `<Routes>`), so it appears on every page — Setup, Login, Monitor, EditConfig, GwConfig.

Renders at `bottom: 6px, right: 8px` with monospace 10px text and opacity `0.7` — visible to anyone who looks for it without blocking form action buttons (Back / Next / Setup / Save) or scroll. Link target: `https://github.com/autonomous-ai/autonomous-os`.

Reason it exists: HAL Python (`os/hal/`) ships GPL v3, baked into the board image. GPL §6 requires recipients of the binary to be informed where corresponding source lives. The footer satisfies the "written offer" alternative by exposing the public repo URL on the device itself. See also `scripts/release/tag-release.sh` + `Makefile:tag-release` for the version → commit traceability piece.

---

## 9. Build & Deploy

```bash
# Build production
make web-build        # tsc + vite build → os/services/web/dist/

# Deploy to Pi
make web-deploy       # web-build + rsync dist/ → /usr/share/nginx/html/setup/

# Deploy HAL (when server.py changes)
make hal-deploy       # rsync + pip install + systemctl restart hal.service
```

> Deploy uses `PI_HOST=lamp.local` (mDNS). If it doesn't resolve, use IP directly:
> `PI_USER=root PI_HOST=<DEVICE_IP> make web-deploy`
