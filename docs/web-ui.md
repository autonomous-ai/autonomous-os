# Web UI — Monitor Dashboard

## Last updated: 2026-08-25

---

## 1. Overview

The device's Web UI is a React SPA (Single Page Application) built with **React 19 + TypeScript + Vite + Tailwind CSS 4**, serving two purposes:

1. **Setup flow** — WiFi, LLM provider, messaging channel onboarding (`/setup/*` pages)
2. **Monitor Dashboard** — Real-time device status monitoring (`/monitor`)

Build output (`dist/`) is served by nginx at root `/` on the device.

During initial setup, **Channels** is optional and defaults to **Not now**.
Selecting Telegram, Slack, or Discord reveals its credential fields, but the
operator may leave them blank or configure a channel later in Settings.

### 1.1 Browser Tab Title

The browser tab title (`document.title`) reflects the focused page/tab so multiple device tabs are distinguishable. Driven by the shared `useDocumentTitle` hook (`system/web/src/hooks/useDocumentTitle.ts`); format is `Lamp · <segment>[· <sub-segment>]`.

| Route / state | Title |
|---------------|-------|
| `/setup` (and `/` when not provisioned) | `Lamp · Setup` |
| `/monitor#<section>` (active section) | `Lamp · <section label>` — e.g. `Lamp · Chat`, `Lamp · Overview`, `Lamp · Info`, `Lamp · Flow`, `Lamp · Users`, `Lamp · Camera`, `Lamp · Sensing`, `Lamp · Analytics`, `Lamp · Servo`, `Lamp · Logs`, `Lamp · CLI` |
| `/setting#<section>` (Settings, active section) | `Lamp · Settings · <section label>` — e.g. `Lamp · Settings · General`, `Lamp · Settings · Wi-Fi`, `Lamp · Settings · AI Brain`, `Lamp · Settings · Language`, `Lamp · Settings · Voice`, `Lamp · Settings · My Voice`, `Lamp · Settings · Face`, `Lamp · Settings · Channels`, `Lamp · Settings · MQTT`, `Lamp · Settings · Timezone` |
| `/gw-config` | `Lamp · GW Config` |

The static `<title>Lamp Setup</title>` in `index.html` is the pre-mount fallback; the hook overrides it once React mounts and reverts to the previous title on unmount.

### 1.2 Login password link

The login page accepts a password in the URL query for controlled direct access:
`/login?password=<URL-encoded-password>`. The same parameter works on every
protected route and its legacy aliases (`/`, `/monitor`, `/setting`, `/edit`,
`/gw-config`, and `/dashboard`), for example
`/setting?password=<URL-encoded-password>#voice`.
When present, the page fills the Admin Password field and immediately submits
the login form. The auth gate carries it to Login, then returns to the clean
target path and hash after a successful login. The `password` query parameter
is removed with `safeSearch`, so the secret is not left in the displayed URL or
browser history entry. The query must come before `#`; text after `#` is a
client-side fragment, not a query string.

Use this only with trusted, short-lived links: a password carried in a URL can
be exposed through copied links, browser history, and server/proxy logs before
the page removes it.

---

## 2. Directory Structure

```
system/web/
├── src/
│   ├── pages/
│   │   ├── Monitor.tsx        # Dashboard monitor (main file)
│   │   └── ...                # Setup pages
│   ├── components/
│   │   └── ui/                # shadcn/ui components
│   ├── lib/
│   │   └── i18n.ts            # UI string localization (en/vi/zh-CN/zh-TW, English fallback)
│   ├── index.css              # Global styles + theme variables
│   └── main.tsx
├── vite.config.ts
└── package.json
```

---

## 3. Monitor Dashboard (`/monitor`)

### 3.1 Overall Design

Monitor uses a dedicated dark theme with class `.lm-root` (defined in `index.css`), **not using Tailwind** — all styling uses inline styles with CSS variables `--lm-*`.

Layout: **Fixed 216px sidebar + flexible main area**, 100vh height.

### 3.2 Sidebar Navigation

4 sections toggled via local state (`section: Section`):

| Icon | Section | Content |
|------|---------|---------|
| ◈ | Overview | Full system overview |
| ⬡ | System | CPU/RAM/Temp details + history |
| ◎ | Workflow | OpenClaw event feed real-time |
| ⬟ | Camera | MJPEG stream + Display LCD |

Bottom of sidebar shows OpenClaw status (online/offline) and last update time.

**Feature search.** A search box (`SidebarSearch`, `system/web/src/pages/monitor/index.tsx`) sits at the top of the rail to tame the long nav. It filters nav leaves by label **or** parent-group name (case-insensitive substring) and honours the same visibility gates as the rendered nav — debug-only sections (`PUBLIC_SECTIONS`) and absent-hardware tabs (`sectionVisible`) never appear in results. While a query is active the grouped tree is hidden and replaced by a flat result list; each row reuses `.lm-snav-item` so the amber active/hover treatment carries over, and shows a small parent-group chip (e.g. `General` · `Settings`). The leading magnifier turns amber on focus; a trailing clear (×) button appears once there's a query (also cleared by `Esc`). `Enter` jumps to the first result.

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

Settings is **not a separate page**. It is an area of the same Monitor shell (`system/web/src/pages/monitor/index.tsx`), reached at the `/setting` route. In `App.tsx`, `/monitor` and `/setting` are child routes of a single layout route whose element renders `<Monitor/>`; React Router keeps that element mounted while only the matched child path changes, so the sidebar does **not** remount when switching between Monitor and Settings (no full-page flash). The shell derives its area — `"monitor"` or `"setting"` — from `useLocation().pathname`.

The Settings collapsible group lives in the shared sidebar `NAV` (`system/web/src/pages/monitor/types.ts`). Clicking a Settings leaf navigates to `/setting` and renders `SettingsPanel` (`system/web/src/pages/settings/SettingsPanel.tsx`) in the main area; clicking a Monitor leaf navigates to `/monitor`.

**URL hash scheme** — the in-memory section keeps internal `settings:*` ids, but the URL hash uses short labels in the setting area (helpers `sectionToHash`/`hashToSection` in `types.ts`):

| Leaf | URL |
|------|-----|
| General | `/setting#general` (internal `settings:device`) |
| Wi-Fi | `/setting#wifi` |
| My Voice | `/setting#voice` |
| Face | `/setting#face` |
| AI Brain | `/setting#llm` |
| Runtime | `/setting#runtime` |
| Language | `/setting#stt` |
| Voice | `/setting#tts` |
| Realtime | `/setting#realtime` |
| Channels | `/setting#channel` |
| MQTT | `/setting#mqtt` |
| MCP Tools | `/setting#mcp` |
| Plugins | `/setting#plugins` |
| Timezone | `/setting#timezone` |

Monitor leaves serialize as the plain id, e.g. `/monitor#overview`, `/monitor#system`, `/monitor#flow`. Defaults: `/monitor` with no/invalid hash → `overview`; `/setting` with no/invalid hash → `general` (URL normalized to `/setting#general`). Deep-links (e.g. `/setting#wifi`) and browser back/forward are honored via a `useLocation`-driven effect. Non-debug users only see the leaves in `PUBLIC_SECTIONS` (which includes Chat, Overview, Info, Flow, Camera, Users, Bluetooth, **Logs**, **CLI**, and the public Settings leaves General/Wi-Fi/My Voice/Face/MCP Tools/Plugins/Timezone); `?debug=true` reveals the rest (Sensing, Analytics, Servo, API Docs, Agent gateway, and the deeper Settings leaves AI Brain/Runtime/Language/Voice/Realtime/Channels/MQTT). Pressing `update` swaps the button for `updating…` immediately — the button never says "OK", which would read as "done" for a request that has only STARTED the install (and, for a component that finishes in seconds, arrived before the row could even show progress). Failures show the server's own reason (`rate-limited, retry in 8s`, `bootstrap unreachable`) rather than a bare "Failed". While an install runs, that row shows `updating…` in place of the button (an install takes tens of seconds — the component stops, is rebuilt and restarts — and a row that just sits there invites a second click, which is how a device once lost its HAL runtime). The `update` buttons in the Overview **Versions** card (Web / OS / HAL / Agent rows, plus Bootstrap and Device in debug) are gated the same way — regular viewers get no one-click OTA trigger. The top-bar **Debug** toggle beside the Dark/Light button toggles that query parameter while preserving the active route hash and any other query parameters; its amber state indicates that debug mode is enabled.

**Speech attention gate** lives in the public **General** settings card, not the debug-only Realtime section. Its checkbox writes the top-level `wakeword` flag; saving restarts HAL so the change applies. When enabled, speech must follow an attention trigger: a spoken phrase, single click, turning toward the lamp while speaking, or an enrolled person entering view (`presence.enter`). A stranger-only enter does not open the voice gate unless the deployment sets `HAL_PRESENCE_WAKE_STRANGERS=true`. The card lists the currently accepted **spoken** phrases, including the active agent's exact current name and the permanent `autonomous` and device-type aliases; the system manages that list. Reload Settings after an agent rename to see the new name. When disabled, every utterance is handled without a trigger.

**Timezone** (`/setting#timezone`, internal `settings:timezone`, `TimezoneSection.tsx`) — an admin-gated section that, like Agent Runtime, is **not** part of the form's "Save Changes" flow: it has its own **Apply** button. It loads the current zone and the selectable IANA zone list via `GET /api/device/timezone`, lets the operator pick a zone from a single dropdown (`<select>` grouped by region via `<optgroup>`, each option labelled `(GMT+7) Ho Chi Minh` and ordered by UTC offset, the way common web timezone pickers work), and shows a live preview of the local time in the selected zone. On **Apply** it calls `POST /api/device/timezone {timezone}`; the change applies immediately (no device restart needed).

The legacy standalone `/edit` page was removed; its `SettingsPanel` is now reachable only through the `/setting` tabs inside Monitor. `/edit` (and the Setup "update →" hint) now redirect to `/setting`.

---

#### Voice — the Piper panel

Every button in this panel sets `type="button"`. The panel renders inside the
settings `<form>`, where a button defaults to `type="submit"` — so Download, Use
and Remove each submitted the whole settings form, saving the config and
restarting HAL underneath the very request they had just fired. That is what
killed downloads mid-transfer, lost Remove clicks to a 502, and made the device
say *"Be right back"* on a click that was only supposed to touch `/opt/piper`.
The symptom looked like three unrelated bugs and was one missing attribute.

`TTSSection` (`system/web/src/pages/settings/TTSSection.tsx`) gains a fourth
provider, **Piper (Local — free)**. It is unlike the other three in that it has
no base URL and no API key, so selecting it hides both fields, and hides the
language filter as well — a Piper voice *is* a language, so filtering the list
would only conceal models the operator installed on purpose.

In their place the panel renders the install state, in the order the dependency
actually runs: the engine first, then voices. Voice rows stay hidden until the
engine exists, because downloading a 63 MB model the device cannot yet load is
63 MB wasted. Each row shows its licence next to the model name — every voice
offered is safe to ship, but the licences differ, and the person choosing one
should be able to see which.

Progress polls every two seconds and **only while a job is running**; this is
the one part of the page whose state changes without the operator touching
anything. Downloads happen on the device, so the panel reads `job` out of
`GET /api/voice/piper/status` rather than tracking anything itself.

A running download gets **its own row** — name, bar, and `13.2 / 60.3 MB · 24%`
— rather than a percentage on the button it was started from. On a domestic
connection 63 MB takes minutes, and for all of them this is the only thing on
the page that is happening. The byte counter is there because a percentage
barely moves on a slow link; bytes visibly do, which is the difference between
*downloading* and *stuck* to whoever is watching. The row also says the download
runs on the device and survives leaving the page or reloading, which is true and
otherwise not guessable.

The panel adopts the job returned by the POST instead of waiting for its next
poll to discover it. That is what makes the button react to the click at all.

An installed voice offers **Use** and **Remove**; the one in use offers neither,
because switching has to come before deleting. Remove takes two presses — the
first turns the button into *Confirm* — since a 63 MB model costs minutes to
fetch again and an inline confirm keeps that decision in the row rather than
behind a browser dialog. The second press updates the row **immediately**, then
reconciles: a button that stays put after a deliberate confirm reads as the
click not having registered, and the round trip is seconds long whenever HAL is
restarting.

It does this by **masking** the polled status for that voice, not by editing it.
The status poll keeps running throughout the removal, and each poll reports the
voice as still installed until the delete lands — an edited copy was simply
overwritten by the next poll two seconds later, putting the Remove button back
and making the confirm look ignored. The mask lifts only once fresh status has
arrived. Removing a voice does **not** restart HAL; only saving the voice
configuration does. Remove is also hidden on the last installed voice, since HAL
would refuse it — better than a button that answers with a refusal ten seconds
later.

For Piper the Voice dropdown is filled from the **panel's own status**, not the
page-level voice fetch. The panel polls HAL, so the list follows a download or a
removal the moment it finishes, and a failed poll leaves the previous answer in
place rather than blanking the picker.

When a panel action fails because HAL is restarting — every voice save triggers
one, and a click landing in that window is simply lost — the panel says the
device was restarting and **nothing changed**, rather than only "reconnecting".
Reporting a reconnection alone let the operator believe the voice had been
removed when it had not. Download and Remove are also disabled while the device
is unreachable, so the click cannot be dropped in the first place.

The engine line appears **only while the engine is missing**. Once installed it
states nothing the voice list below does not already imply, and a permanent
green tick on a finished setup step is just something to read past every time.

Two details worth keeping if this code is refactored. The preview language is
derived from the voice name (`vi_VN-…` → `vi`) rather than from the language
filter, which Piper hides — without that the Test Voice button sends the
device's STT language and speaks an English sample line through a Vietnamese
model, which sounds like a broken voice rather than a mismatched one. And
attribution is deliberately *not* surfaced here: it is owed by whoever
distributes a voice, not by the person switching one on, and `CREDITS.md` is
what discharges it.

**Test Voice is blocked, not failed, until the selected voice is on the
device.** Pressing it mid-download reaches a backend that has no model to load,
and the honest answer — a 503 — arrives at the operator looking like the API
fell over. The button instead greys out and says what is happening (*That voice
is still downloading*, or *Download a voice first* when nothing is selected).
Switching the provider to Piper never invents a voice name either: it selects
one from the installed list or leaves the field empty, because a name that is
saved but absent configures the device for a model it cannot load.


## 4. Polling & Data Sources

Monitor polls system/HW APIs every **3 seconds**. Flow uses file-backed hybrid mode: REST seed + live stream.

### 4.1 OS Server (Go, port 5000, prefix `/api`)

| Endpoint | Data |
|----------|------|
| `GET /api/system/info` | CPU load, RAM (KB), temperature, uptime, goroutines, version, deviceId, capabilities (declared capability names — both the Monitor and the Edit/Settings page gate hardware tabs on these; see the shared `useCapabilities` hook) |
| `GET /api/system/network` | SSID, IP, public IP, Tailscale IP, signal (dBm), internet (bool), pingMs (internet probe RTT, 0 = unmeasured) |
| `GET /api/agent/status` | active runtime name, connected (bool), sessionKey (bool), version, emotion, uptime (OS server connection uptime, secs), agentUptime (runtime process uptime when supplied, secs — survives OS server restarts). The Agent row in the Versions card probes its CLI version asynchronously and retries transient boot-time failures. |
| `GET /api/agent/recent` | Latest flow events from today's JSONL file (`local/flow_events_<date>.jsonl`) |
| `GET /api/agent/flow-events?date=YYYY-MM-DD&last=500` | File-backed flow events API used for Flow seed/history |
| `GET /api/agent/flow-stream` | File-backed live stream (SSE) for Flow updates when JSONL changes |
| `GET /api/agent/events` | Monitor bus SSE endpoint (kept for compatibility) |
| `GET /api/logs/tail?source=bootstrap&lines=N` | Authenticated Bootstrap log tail used to seed and manually refresh the Bootstrap Logs tab. |
| `GET /api/logs/stream?source=bootstrap` | Authenticated SSE stream for live Bootstrap Logs-tab updates. |
| `POST /api/agent/restart` | "Start + enable + restart" recovery: backend does best-effort `systemctl enable <unit>` (so the fix survives reboot) then calls the runtime's own `RestartAgent()` (which resolves to `systemctl restart <unit>` — starts if stopped). Powers the Agent Gateway card's small restart icon at bottom-right. |
| `POST /api/system/force-update` | Triggers OTA check via bootstrap worker (proxies to `localhost:8080/force-check`) |
| `GET /api/system/ota-versions` | Per-component `{current, target, min_version, update_available, held_by_floor}` (proxies bootstrap `/versions`, including the installed device profile from `devices.<device_type>`, plus an `agent` alias for the configured runtime's CLI). The Versions card shows an `update` button wherever `update_available` is true (`held_by_floor` is reported but NOT used for the button: the button installs the published version on this device, like `software-update <key>` over SSH, and the floor only stages the automatic fleet rollout) |
| `GET /api/system/ota-updating` | Components the worker is installing right now (`{updating: [...]}`, plus the `agent` alias). Deliberately cheap — no metadata fetch — because the Versions card polls it every 2 s while an install runs and shows `updating…` on that row instead of the button |
| `POST /api/system/software-update/:target` | Per-component OTA check. `target`: `os-server` \| `bootstrap` \| `web` \| `hal` \| `device` \| `agent`. Bootstrap self-updates by spawning the installer in the background, so its replacement can safely restart the worker; `device` installs the resolved `devices.<device_type>` profile. **`agent` is virtual** — os-server resolves it to the configured runtime's CLI (`codex`/`claudecode`/`opencode`/`picoclaw`) so the browser never needs to know which runtime runs; `hermes` returns 400 (it cannot be pinned, so bootstrap never auto-applies it). Rate-limited to one call per target per 30 s |

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
| `GET /hw/voice/mic-level` | SSE stream (~10Hz): level (voice-mic RMS, int16 scale), threshold (VAD), active, muted, sensing_level / sensing_age_s / sensing_threshold (noise mic — last SoundPerception sample, null when sensing is down), tts_speaking / music_playing (live playback state — the audio card flips "Speaking…/Playing music" off the stream instead of waiting out the 5s status poll) |
| `GET /hw/led/color` | led_count, color [R,G,B], hex (#rrggbb) |

---

## 5. Section Details

### 5.1 Overview Section

Cards included:

**OpenClaw AI**
- Connected/disconnected status
- Agent name
- Session key: Acquired / Pending
- **Restart icon** at the bottom-right of the card (small `RotateCw` button, 24×24). Prompts a `confirm()` then POSTs `/api/agent/restart` — backend does "start + enable + restart": (1) `systemctl enable <unit>` best-effort so the fix survives reboot, (2) runtime `RestartAgent()` → `systemctl restart <unit>` which starts the service even if it was stopped. Icon spins while the request is in flight; `OK` / `Failed` label appears for ~2.5s. Used to recover a stopped+disabled gateway without SSH.

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
- **Mic level VU meters** (under the volume slider), fed by the `GET
  /hw/voice/mic-level` SSE stream (~10Hz, via the `/api/hardware` proxy);
  raw RMS is mapped to percent on a dBFS scale (-60dBFS → 0%, 0dBFS → 100%)
  and each bar carries an amber tick at its trigger threshold plus a numeric
  `live RMS / threshold` readout on the right of its label:
  - **Mic level** — the voice-pipeline (STT) mic; pumps live as the user
    talks into the device. Tick = VAD wake threshold (speech must peak past
    it for the device to start listening). Drops to 0 while TTS/music plays
    (mic is draining); dimmed with a "muted" hint while the mic is muted.
  - **Noise mic** — the sensing mic (SoundPerception): one 0.5s RMS sample
    per sensing poll, so this bar steps every few seconds instead of
    pumping (samples also pause during/after TTS). Tick = loud-noise
    threshold. Hidden when the device has no sound perception running;
    shows 0 when the last sample is older than 60s.
  The stream stays open while the voice mic is muted (the sensing mic is
  independent of the mute switch) and closes while the browser tab is
  hidden.

On phone widths of **480px or less**, the four Overview status cards use one
column. This preserves room for the Audio controls and VU meters, and prevents
the shorter Presence card from being stretched by the taller Audio card.

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

> **Layout & pill clouds.** The device cluster (row 2) splits into two equal
> columns: the right column holds the expressive cards (Emotion, Servo Pose,
> Versions) and the left column holds the compact status cards (Hardware, Scene,
> Buddy); they collapse to one column under ~860px. Versions sits in the right
> column so the two columns balance in height rather than the right ending short
> under Servo Pose. The Emotion preset list and the Servo recording list each
> render as a **pill cloud** — the active pill is hoisted to the front so the
> current state reads first, and the full set free-wraps (no scroll). Both the Emotion
> and Servo Pose cards split into two columns: the current-state summary (Emotion
> emoji + name; Servo current pose + Release button) in a fixed-width left column
> and the preset/recording cloud filling the rest on the right. Under ~360px the
> two columns stack. The Emotion name is always shown in the theme's
> high-contrast text colour, inside a surfaced status pill; the preset's LED
> colour remains a dot, border, and soft tint. This keeps dark presets such as
> `sleepy` readable in dark mode. The summary reserves room for the emoji and
> long names such as `acknowledge`; when a card is narrow, the pill cloud wraps
> below it rather than overlapping the current state.

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

- Initial/history load via `GET /api/agent/flow-events`.
- Live updates via `GET /api/agent/flow-stream` (SSE emitted on file change).
- Fallback polling (2s) is used only if live stream disconnects.
- Displayed turns/events are fully derived from JSONL flow logs.

**Turn Pipeline (SVG)** — Implemented by `FlowDiagram` in `system/web/src/pages/Monitor.tsx`. Full layout (three clusters: OS server / HAL / OpenClaw, column grid, Cron vs OpenClaw, HAL row aligned with Tool, approximate coordinates) is documented in **`docs/flow-monitor.md`**; Vietnamese summary in **`docs/vi/flow-monitor_vi.md`**.

Turn Pipeline grouping behavior:
- Turns are still started by input/trigger events (`sensing_input`, `chat_input`, `schedule_trigger`, etc.).
- The UI now anchors each turn to the first detected `run_id` (from event root or detail payload).
- For user mic actions: each `sensing_input` with `[voice]` / `[voice_command]` (and `voice_pipeline_start`) creates a separate turn even if events share the same `run_id`.
- For typed chat: each `sensing_input` with `[web_chat]` (monitor composer, icon 🖥) or `[mqtt_chat]` (MQTT `chat.send` from a phone app, icon 📱) creates its own boundary turn so it isn't merged with adjacent voice/sensing turns. Both sit in filter category **Web**, with separate sub-type chips (`web` / `mqtt`), and the badge keeps the two apart — the turns are otherwise identical server-side.
- For user chat actions: each `chat_input` (telegram input) creates its own boundary turn, so it won't be merged with adjacent voice turns even if OpenClaw reuses the same `run_id`.
- If a later event has a different `run_id`, Monitor splits it into a new inferred agent turn.
- **Turn type badge** (`motion`, `voice`, …): merged segments that share one `run_id` may include both camera motion and a voice line; the first segment used to win, so the badge could read `motion` while the utterance was voice. After grouping, if any `sensing_input` in the turn is `[voice]` or `[voice_command]`, the badge uses that (voice beats motion for the same run).
- `OUT` text is only taken from `tts_send`/`intent_match` events matching the turn `run_id` (or events without run_id), preventing cross-turn input/output mismatch.
- LLM token usage is shown on LLM nodes (Agent Call / Thinking / Response): `in/out` and, when available from `token_usage`, `cache read/write` + `total`.
- For Telegram input, placeholder summaries like `[telegram]` no longer lock the `IN` field; when a later event with the same `run_id` contains real message text, the UI replaces the placeholder with that text (and will override earlier `sensing_input` text like SOUND within the same UI turn). If the Telegram input message is completely missing (ghost turn), the turn type becomes `unknown` to avoid misleading “TG IN”.
- Temporary fallback: when Telegram text is unavailable, UI displays `Message content from telegram`.
- Turn badges always render the `IN` row; if input is missing, UI shows `Input not captured`.
- **Turn-badge icons** (`FlowSection/TurnBadge.tsx`) — every glyph is a Lucide icon for consistency with the header: the row-1 source icon comes from `TYPE_LUCIDE` (keyed by turn type), `BROADCAST→Megaphone`, duration`→Timer`, queued`→PauseCircle`, audio-debug`→Mic`, pose-bucket`→Armchair`, no-reply`→Ban`, channel-out`→MessageSquare` / TTS`→Volume2`, dropped & queued`→PauseCircle`, closed-stream`→TriangleAlert`, silent`→Moon`, HW`→Lightbulb`, lightbox-close`→X`, View-pipeline`→Workflow`. No emoji glyphs.
- **Per-turn user chip** — the turn's recorded current-user renders via the shared `UserAvatar` (name + enrolled face photo, `UserRound` fallback), the same component the header chip uses, so "who" reads identically in the header and every badge. The photo map (`/face/owners`, name→filename) is threaded down from `FlowSection` as the `userPhotos` prop.
- Flow Panel header uses Lucide icons throughout (brand `Hexagon`, `Summary→ClipboardList`, `Canvas→LayoutDashboard`, `Bundle→PackageOpen`, `Full day→CalendarDays`, `Clear→Trash2`) for a consistent icon set — no emoji glyphs.
- **Current-user chip** — when the device recognizes an enrolled person, the header chip shows that person's **name + face avatar** (first enrolled photo via `GET /face/photo/<label>/<file>`, with `/face/owners` polled every 30s to map name→filename); on `unknown` or a missing/broken photo it falls back to a generic Lucide `UserRound` glyph. The name comes from `GET /identity/current-user` (polled every 5s), **not** `/face/current-user`: that endpoint answers only "who does the camera see", so the chip stayed blank whenever nobody was in frame — and permanently on a device with no camera — even right after speaker-ID recognized an enrolled user. HAL resolves face-then-voice, so the chip shows the face user whenever the camera has one and the recognized speaker otherwise. The value is the normalized label (`long`), so the chip's `capitalize` and the photo lookup behave identically for both modalities — the `Speaker - ` transcript prefix never reaches it. The tooltip says which modality the identity came from (*seen* vs *heard*).
- Flow Panel header actions include **`↓ Bundle`**, **`full day`**, **`🗑 Log`**.
- **`↓ Bundle`** — one click saves **two files**: (1) server JSONL tail via `fetch` + blob (`GET /api/agent/flow-logs?last=500`), (2) UI snapshot JSON (`events` + `groupIntoTurns` → `lamp_flow_ui_snapshot_*.json`).
- **`full day`** — `GET /api/agent/flow-logs` without `last` (whole day JSONL).
- `🗑 Log` asks for confirmation and calls `DELETE /api/agent/flow-logs` to truncate the server flow log, then clears current Flow UI events.
- **Filters modal** (`FlowSection/FiltersModal.tsx`) — the turn-list header keeps only a free-text search box and a **Filters** button (badged `Filters · N` with the count of active filter groups). Clicking it opens a centered modal hosting the full filter set: **Sources** (Mic / Cam / Btn / CH / Web / Cron / Sys quick-toggles, plus Dropped when present), **Sort** (Newest / Oldest / Slowest / Fastest / ↑↓ Tokens), **Sub-types** (per-type toggles with an All-on / Enable-all shortcut), and **Time range** (quick presets Last 15m / 1h / 6h / Today, plus two labeled clock-prefixed From/To pills joined by an arrow; the native `<input type="time">` is de-chromed via `.lm-time-input` and the active bound tints amber). A footer offers **Reset all** and **Done**. The modal renders inside the FlowSection tree (under `.lm-root`) so `--lm-*` tokens resolve in dark and light mode; it closes on overlay click, the ✕, **Done**, or `Esc`. All filter state lives in `FlowSection/index.tsx` and is threaded in as props, so opening/closing never resets a filter.
- **Lucide icons for sub-types** — the source and sub-type chips use Lucide icons (`TYPE_LUCIDE` in `FlowSection/types.ts`, e.g. `voice→Mic`, `cmd→Mic2`, `motion→Eye`, `activity→Activity`, `voice_emo→Speech`, `emotion→Smile`, `web→Monitor`, `sys→Settings`) instead of emoji glyphs, inheriting the chip's `currentColor` and on/off opacity treatment.
- Turn history list shows **all turns** for the day (newest first), derived from the **last 10 000** flow events — covers a full day of typical activity.
- Flow event memory is capped at 10 000 events.
- Telegram stitching heuristic: if a Telegram fallback input turn (without real input text) is immediately followed by an agent-output turn within 30s, Monitor stitches them into one turn so the reply stays with the original Telegram input.

### 5.4 Camera Section

- **Camera Stream**: MJPEG live stream from `GET /hw/camera/stream` (downscaled + throttled; default ~10fps, ~320px width). The `<img>` remounts with a fresh connection (bumped `streamEpoch` cache-buster) whenever the camera transitions to enabled — via the Enable button or an auto-enable picked up by polling — so live video returns immediately without a page refresh. A stream error that lands right after enable (HAL's capture loop needs ~1-2s to deliver the first frame) is not latched: it auto-retries on a short delay until a frame loads.
- **Display Eyes (GC9A01)**: Round 1.28" screen snapshot from `GET /hw/display/snapshot`, displayed as circle with amber glow. Has Refresh button.
- **Camera Snapshot**: Static image from `GET /hw/camera/snapshot`, with Capture button to take new shot.

### 5.5 Logs Section

- Dedicated runtime log panels: HAL, OS (os-server), Buddy, **Bootstrap** (source id `bootstrap`), plus **Agent** and **Agent Service** (source ids `openclaw` / `openclaw-service`).
- **Bootstrap** reads the OTA bootstrap worker's systemd journal (`bootstrap.service`). Its initial load and manual refresh use `GET /api/logs/tail?source=bootstrap&lines=N`; live updates use `GET /api/logs/stream?source=bootstrap` (SSE). Both endpoints require the normal authenticated session.
- The **Agent**/**Agent Service** tabs are runtime-aware — the backend (`resolveLogSource` in `server/logs.go`) points them at whichever agentic backend is active:
  - openclaw: `Agent` → `/var/log/openclaw/agent.log` (falls back to newest `/tmp/openclaw/openclaw-*.log`), `Agent Service` → `journal:openclaw.service`
  - hermes: `Agent` → `/root/.hermes/logs/agent.log`, `Agent Service` → `journal:hermes-gateway.service`
  - picoclaw: `Agent` → `/root/.picoclaw/logs/gateway.log`, `Agent Service` → `journal:picoclaw.service`
  - codex: `Agent` → `journal:codex.service`, `Agent Service` → `journal:codex.service` (the gatewayd bridge has no file log — journal only)
- Each panel streams via SSE (`GET /api/logs/stream?source=<source>`); its initial load and manual refresh read `GET /api/logs/tail?source=<source>&lines=N`.
- Supports level filtering (ALL/DEBUG/INFO/WARN/ERROR) and text/regex search.

> **Note**: Camera serves a dual role — (1) live stream display for user viewing, (2) automatic sensing data source. Sensing service reads a frame from camera every 2s to detect motion, faces (Haar cascade), and light level. When significant events are detected (person appears, large motion), a full-resolution JPEG auto-snapshot is sent with the event to OpenClaw AI for vision analysis.

### 5.6 Chat Section

Interactive chat interface for communicating with the agent. Layout: sidebar (conversation list) + main chat area.

**Conversations**
- Multiple conversations stored in localStorage (max 50, 200 messages each).
  Image attachments are too large for the localStorage quota, so their
  data-URLs are stripped on save and persisted separately in **IndexedDB**
  (`lib/chatImageStore.ts`, keyed by message id); a mount effect re-attaches
  them after reload and prunes entries whose message no longer exists.
  Deleting a conversation (or Clear/history-TTL) also deletes its stored
  images.
- Sidebar with search, pin, rename (double-click), delete (double-click confirm), export as TXT
- Grouped by date: Today / Yesterday / This week / Older, pinned at top. Each group header shows a hairline divider and an item count.
- Each row shows a deterministic on-palette avatar dot (hashed from the conversation id), the title, a localized relative timestamp (`now` / `5m` / `2h` / `yesterday` / `3d`, hidden on hover), and a last-message preview. The active conversation is marked with an amber left rail.
- Keyboard shortcut: Cmd/Ctrl+N for new chat
- Collapsible sidebar

**Message Input**
- Textarea with Shift+Enter for multi-line, Enter to send
- **"+" menu** (`chat/PlusMenu.tsx`) at the composer's left edge. Opens upward (the composer is pinned to the bottom); closes on outside click / Escape, and is derived closed while a turn is sending. Entries:
  - **Attach file** — the former paperclip button, now folded into the menu. Drag-drop and clipboard paste still attach directly without going through the menu.
  - **Skills** ▸ — fly-out sub-menu with the four surfaces below, a rule separating the two that add a skill of your own (Write / Upload) from the two that work with skills that already exist (Browse / Manage). Each opens a portalled modal (`chat/ModalShell.tsx`, shared shell + `chat/styles.ts` field styles); the rows themselves are `chat/MenuPanel.tsx`, shared with the Manage skills header menu.
- File/image attachment (max 10 MB): "+" → Attach file, drag-drop, clipboard paste
- Messages sent via `POST /api/sensing/event` with `type: "web_chat"`. The handler tags the run via `MarkWebChatRun(runID)` so the agent reply is suppressed at TTS (rendered in this UI only) and skips the physical wake greeting / opening filler. An **image** attachment rides the payload's `image` field (raw base64); the handler (1) saves it to `/tmp/web-chat-*.jpg` and appends an `[image: <path>]` tag so tools can read the file directly (e.g. face enrollment), and (2) runs the describe-first gate in `system/vision` (see `docs/realtime-voice.md`, "Frame handoff"): a text-only main model gets an `[image description]` line produced by the catalog's vision model, a vision-capable one gets the raw attachment. Both steps run BEFORE the agent-busy queue fork, so a queued turn replays with the description already inlined.
- **Non-image attachments** ride only the separate `file` field — `{name, mime, content}`, base64 — never the `image` field; `agentfile.SaveInbound` handles them. They land in `/tmp` with their **real** extension and the turn carries `[file: <path> (<name>)]`. Two fields rather than one because the handling is opposite: an image must go through the describe-first vision gate, a document must not. Until this split, everything the composer accepted was sent in `image` and written as `/tmp/web-chat-*.jpg`, so attaching a PDF produced a file mislabelled as a photo that then failed the vision gate — the composer accepted any file type, but only images actually worked. The client's `name` is used only for its extension (validated to a short alphanumeric suffix, else `.bin`) and the display label; the written filename is generated, so a name like `../../etc/passwd` cannot steer the write. Capped at 10 MB decoded, matching the composer's own check. The MQTT `chat.send` path carries the identical field and re-enters this same handler over loopback, so a phone and a browser attach files through one implementation (`docs/mqtt.md`).
- **Files coming back OUT of a turn** (`chat/AgentFiles.tsx`). A turn can only *name* a file it produced — "take a photo" ends with an absolute device path like `/root/.openclaw/media/hal-snapshots/snap_*.jpg`, which a browser cannot read. Each finished agent message is scanned for such paths and every hit is rendered beneath the bubble: an inline image, or a download chip for anything else, both pointing at `GET /api/agent/file?path=…`.
  - **Three places are scanned, not just the reply text.** Asked to send a photo, an agent typically calls its channel tool — `message {"action":"send","media":"/root/.openclaw/media/…jpg"}` — and its spoken reply names no path at all, so text-only detection finds nothing. Tool **args** carry it (the server logs them untruncated in the flow event's `detail.args`; only the chip's display is shortened), and a `curl /camera/snapshot` puts it in the tool **result** instead.
  - **Detection is client-side, enforcement is not.** Scanning in the browser costs no hook in the turn pipeline and works on conversations already in localStorage, which a server-side scan at turn-end could never reach. The roots in `AgentFiles.tsx` are a filter that stops the UI firing requests that could never succeed — never a permission. A path that is gone or refused simply drops the attachment (`onError`) and leaves the path readable as text.
  - Distinct from `/api/sensing/agent-snapshot/<runtime>/<source>/<name>` (`camera_snapshot.go`), which Flow Monitor uses for tool-result snapshots: that one serves what the *device* resolved from path segments, this one serves what the *agent* named.

**Skills menu (composer "+" → Skills)**

| Item | File | State today |
|------|------|-------------|
| **Create with Agent** | `chat/PlusMenu.tsx` | Prefills the chat composer with the skill-creator prompt and focuses it; does not send the message. |
| **Write skill** | `chat/WriteSkillModal.tsx` | Three-field form — Skill name / Description / Instructions — matching a `SKILL.md` (name + description → front-matter, instructions → body). Saves via `POST /api/agent/skills`; on success the modal shows the path it wrote. See "Writing + installing skills" below. |
| **Upload a skill** | `chat/UploadSkillModal.tsx` | Installs a `.skill`/`.zip` the operator picked from their own machine — drop zone or file picker, 16 MB client-side cap matching the device's. Same destination and the same replace-on-name-clash semantics as the store Install button; only the source of the bytes differs. |
| **Browse skills** | `chat/BrowseSkillsModal.tsx` | Live against the Autonomous Agent Skills catalog — see "Skill catalog" below. |
| **Manage skills** | `chat/ManageSkillsModal.tsx` | Skills present in the active agentic runtime's skills dir (`GET /api/agent/skills`), two views: a search box plus a **three-column list** — skill (`/music` with its description beneath), file count, last updated — then a detail view rendering the same two-pane file browser Browse skills uses. A list rather than Browse's card grid, because for something already installed the useful question is what's here and when it last changed, which reads better as aligned columns. "Last updated" is a fixed `MM/DD/YYYY` date — the column exists to spot which skills are stale, and a fixed-width absolute date compares down a column at a glance where relative units ("3d ago" over "12m ago") don't; the order is hardcoded rather than locale-derived so the column always lines up and the same screenshot can't read as a different day. The exact timestamp is on the row's tooltip. Search filters **client-side** over name + description — unlike Browse, whose keyword goes to the catalog, `ListSkills` already returned the whole set, so there is nothing to ask the device for. Everything the runtime has appears regardless of origin — authored, store-installed, role-bundled and OTA-pushed skills share one tree. Reload button; an empty list reads "no skills installed yet", distinct from the 501 a runtime that can't list returns. The detail view's footer carries **Uninstall**, which is two-click: the first arms it and states what will be deleted, the second commits. On success the list refetches so the removed skill can't linger. The list header carries a **New** dropdown immediately left of the close button, repeating the composer menu's Write skill / Upload a skill — both open *on top of* this modal rather than replacing it, and closing either refetches the list, so an operator adds a skill and lands back on the (refreshed) list. Escape closes only the front-most shell. |

The **New** dropdown also includes **Create with Agent**. It closes Manage skills, focuses the chat composer, and pre-fills `Let's create a skill together using your skill-creator skill. First ask me what the skill should do.` It does not send the message, so the owner can review or edit it first.

**Skill catalog (Browse skills)**

The catalog is the public read API of `bff-web-service` (`agent-skills-public-api.md`), wrapped device-side by `system/server/agent/delivery/http/handler_skills.go`. Both hops go through os-server, never the browser — same rationale as `GET /api/plugin/browse`: no CORS round-trip and the catalog host stays server-side. Base URL defaults to `https://apiv2.autonomous.ai`, overridable with `SKILL_STORE_BASE_URL`; every upstream call carries the `location: en-US` header the catalog's middleware requires.

| Device endpoint | Upstream | Notes |
|-----------------|----------|-------|
| `GET /api/agent/skills/browse` | `GET /api/v1/agent-skills` | Forwards `keyword` / `category_id` / `plan` / `page` / `limit`. `status` is deliberately **not** forwarded — upstream can't tell "unset" from `0`, so sending it would silently filter the listing. Returns `{data: [Skill], total}` (`domain.StoreSkillList`). |
| `GET /api/agent/skills/bundle?id=<id>` | `GET /api/v1/agent-skills/:id/download` | Downloads the `.skill` archive to a temp dir, unzips it there, and returns `domain.SkillBundle` — the file list with UTF-8 contents inlined. The temp dir is removed before the response is written: this is a **preview**, nothing is installed. |

The catalog returns business failures as **HTTP 200 with a non-1 `status`**, so the proxy checks the envelope status, not just the HTTP code, and surfaces the upstream message as a `502`. The id rides a query param rather than a path segment so the route can't collide with the sibling static `skills/browse`.

Extraction is hardened: zip-slip guarded (any `..` or absolute entry fails the whole bundle), `.DS_Store` / `__MACOSX/` filtered, and capped at 16 MB per archive, 2 MB per file, 512 KB inlined as text (longer files are marked `truncated`), 500 files. Non-UTF-8 entries come back flagged `binary` with metadata only.

UI: the modal is two views. The **list** searches server-side (300 ms debounce on the `keyword` param, 50 per page) and lays results out as a responsive grid — two cards per row, collapsing to one below ~250 px per column — each showing the slash-prefixed name (`/algorithmic-art`, matching the installed listing) plus plan chip, then the author directly beneath it, then the description, then the compatibility chips. No version and no chevron on the card: the version means nothing until the skill is opened (the detail header still carries it) and the whole card is the click target. Clicking a card opens the **detail** view — a wider shell with the archive's files on the left and the selected file's content on the right, `SKILL.md` selected by default, and an **Install** button in the footer. The header's back arrow (and Escape) returns to the list instead of closing the modal.

**Upload file requirements** (enforced device-side, so a malformed upload is a `400` rather than a skill the agent can never load). Mirrors the upstream format — see [anthropics/skills' algorithmic-art](https://github.com/anthropics/skills/tree/main/skills/algorithmic-art):

| Input | Requirement |
|-------|-------------|
| `.md` | YAML front-matter must carry **`name`** and **`description`**. That `name` is the directory the skill installs as, so there is nothing to infer from the filename. |
| `.zip` / `.skill` | Must contain **`SKILL.md` at the skill root**. A single common top-level folder names the skill and is stripped; a flat archive falls back to the filename stem via `skills.SlugifySkillName`. |

`skills.ParseSkillFrontMatter` is the one reader for that header. Keys beyond name/description are **tolerated** (the upstream example carries `license:`), only top-level keys count (a `name:` nested under `metadata:` is ignored), and the listing shares the same scanner through a lenient wrapper — there a missing `name:` is fine, since the directory already supplies it. The `SKILL.md` check runs against the **staging** copy, so a rejected archive never touches the live tree.

**Reading, writing + installing skills (per-runtime, via the AgentGateway)**

All three paths go through the agent abstraction — the device layer never hardcodes a skills directory, because each agentic runtime keeps its own:

| Device endpoint | Gateway method | Behaviour |
|-----------------|----------------|-----------|
| `GET /api/agent/skills` | `ListSkills() ([]InstalledSkill, error)` | Walks the runtime's skills dir: one entry per skill directory with its file tree, sorted by name, dirs before files. Description is read from the SKILL.md front-matter. `updated_at` (Unix seconds) is the **newest mtime anywhere in that skill's tree**, not the skill directory's own — a directory's mtime only moves when files are added or removed, so it would report an edited SKILL.md as unchanged; it rides along with the same walk rather than taking a second pass, and falls back to the directory's mtime for an empty skill. A **missing** skills dir (un-provisioned runtime) is an empty list, not an error. |
| `GET /api/agent/skills/files?name=<skill>` | `ReadSkillFiles(name) ([]SkillBundleFile, error)` | One installed skill's files, flat, with UTF-8 text inlined. Returns the **same `domain.SkillBundle` envelope** as the store preview so both detail views render through one component. A skill that's gone (stale listing) is a 404. |
| `POST /api/agent/skills` | `SaveSkill(domain.SkillDraft) (path, error)` | Writes an authored `<name>/SKILL.md`. **Refuses to overwrite** an existing skill (`skills.ErrSkillExists` → 400) so a store- or OTA-installed skill can't be destroyed by an authoring mistake. |
| `DELETE /api/agent/skills?name=<skill>` | `DeleteSkill(name) (path, error)` | Removes the skill directory and everything under it. **Not idempotent**: a skill that isn't installed is a `404` (`skills.ErrSkillNotFound`), not a silent success, so a stale caller learns its list was out of date. Also refuses when something at that path isn't a skill directory. |
| `POST /api/agent/skills/upload` | `InstallSkillArchive(...)` / `InstallSkillMarkdown(content)` | A skill from the operator's machine, **multipart** field `file` (not the base64-in-JSON used for face enrollment: that carries a small JPEG, a skill archive runs to megabytes and base64 would inflate it a third). Capped at 16 MB. Accepts `.zip`/`.skill` **or** a bare `.md`, and enforces the upstream format's requirements — see below. |
| `GET /api/agent/file?path=<abs>` | — (no gateway call) | Serves a device-local file the agent named, so the chat can show it. `path` is **client-supplied and treated as hostile**: two independent gates, either enough to refuse — it must resolve (`EvalSymlinks`, so `..` and symlink escapes both fail) inside an allow-listed root, and its extension must be a served type. Both gates live in **`system/agentfile`**, not in the handler: the MQTT path that pushes the same files to a phone (`chat.file`, see `docs/mqtt.md`) has to agree with this endpoint exactly, and an allow-list with two copies is two chances to widen one by accident. Roots are `media/` + `workspace/` per runtime plus `/tmp` — deliberately NOT a runtime's config dir, whose JSON holds gateway tokens. `.json` and `.log` are **not** served for the same reason. Directories, non-regular files and anything over 32 MB are 404; wrong type or wrong root is 403. Images/PDF go out `inline`, the rest `attachment`, always with `nosniff`. |
| `POST /api/agent/skills/install` | `InstallSkillArchive(archivePath, fallbackName) (dir, error)` | Device downloads the catalog `.skill` archive to a temp dir, then the runtime extracts it into its skills dir. **Deliberately replaces** an existing skill of that name — installing is an explicit user action. |

Both detail views — store preview and installed skill — are the same component, `chat/SkillFilesView.tsx`: files on the left (containing dir dimmed above the basename), the selected file's content on the right, `SKILL.md` open by default, binary entries shown as "no preview". The backend makes that possible by returning one shape for both sources; `skills.BuildFilePreview` is the single place that decides text-vs-binary and truncation, whether the bytes came from a zip entry or off disk.

The shared work lives in `system/skills`: `list.go` walks a skills dir, `read.go` reads one skill's files, `authored.go` renders + writes the SKILL.md, `install.go` extracts an archive. Only the **target directory** differs per backend, the same reason the per-runtime skill watchers are near-copies — so each backend's `save_skill.go` is three one-liners over its own path:

| Runtime | Skills directory |
|---------|------------------|
| openclaw | `{OpenclawConfigDir}/workspace/skills` — shared with `InstallRoleSkills` / `EnsureMCPSkill` |
| picoclaw | `{picoclawWorkspaceDir}/skills` |
| codex | `codexSkillsDir` (`~/.codex/skills`) |
| claudecode | `claudecodeSkillsDir` (`~/.claude/skills`) |
| opencode | `opencodeSkillsDir` (`$XDG_CONFIG_HOME/opencode/skills`) |
| hermes | **writes** → `~/.hermes/skills/authored`; **lists** → that plus `~/.hermes/skills/openclaw-imports` |

Hermes is the only backend that namespaces its skills dir, so it is the only one that needs more than one path. Device writes deliberately stay **out of `openclaw-imports`**: `presync.sh` §0 restores the imported platform skills by running `claw migrate` *only when that dir is empty*, so an authored skill dropped in there would make the guard permanently see a populated dir and a factory reset would silently never restore them. `ListSkills` merges both roots via `skills.ListInstalledFrom`, device-owned root first so a user's skill isn't masked by an import of the same name. Hermes discovers skills anywhere under `~/.hermes/skills`, so the new root needs no config change.

The listing skips `<name>.new` / `<name>.old` (InstallSkillArchive staging + backup) and dot-directories: implementation detail, not skills. The tree walk is bounded at depth 6 and 200 entries per directory so a pathological tree can't produce an unbounded response, and one unreadable skill degrades to an empty tree instead of blanking the whole list.

`ErrNotSupportedByRuntime` → **HTTP 501** naming the active runtime remains the contract for a backend that can't do one of these, and the UI renders it inline — but as of now every shipped runtime implements all three, so 501 is only reachable by a future backend.

Archive handling in `skills.InstallSkillArchive`: a single common top-level directory in the archive names the skill and is stripped (the catalog's `.skill` bundles are shaped `<name>/SKILL.md`); files at the archive root instead use the caller's fallback name (OTA-style zips). The extract is staged in `<skill>.new` and swapped in only on full success, with the previous version moved to `<skill>.old` and restored if the swap fails — a corrupt download can never leave a half-installed skill or destroy a working one. Zip-slip guarded, `.DS_Store` / `__MACOSX/` filtered, capped at 500 files and 4 MB per entry (enforced with a `LimitReader`, so a lying `UncompressedSize64` can't fill the disk).

Neither path restarts the runtime: backends with a skills dir pick new files up per session, the same contract `InstallRoleSkills` relies on.

**Real-time Streaming**
- **Thinking indicator**: collapsible purple block showing LLM reasoning tokens as they stream in (`thinking` events). Click to expand full text (max-height 200px scrollable). Auto-hides on response completion.
- **Assistant delta streaming**: response text appears token-by-token via `assistant_delta` events, instead of waiting for final response. Fallback to `chat_response` partial events for non-agent paths.
- **Tool call chips**: teal badges showing tools the agent invoked during the response (emotion, LED, servo, audio, etc.). Displayed above the message bubble during streaming and persisted on completed messages. A single tool renders as one chip; **two or more collapse into a summary pill** ("N steps" with stacked tool icons + a live/`DONE` marker) that expands on click to reveal the individual chips.

**Response Handling**
- Tracks response by `runId` correlation across SSE events
- Inline HW control markers (`[HW:/emotion:...]`) stripped from displayed text; the markdown-link form some LLMs emit (`[label](HW:/led/off:{})`) is also stripped, keeping the label. Both strip patterns mirror the os-server executor grammar exactly — a malformed variant the executor won't fire stays visible as raw text
- 120-second timeout: if streaming text received, shows partial text; otherwise shows error with retry button
- **Pending-turn recovery across reload**: messages persist an epoch `ts`; a pending reply bubble younger than 10 minutes survives a page reload instead of being finalized as an error. On the first render with the Chat tab active, the UI re-attaches to the stored `runId` and the reply is backfilled from the flow JSONL replay (`/api/agent/flow-stream` re-sends the last 500 events of the day on every connect — `tts_send` / `tts_suppressed` / `no_reply`). If nothing resolves the run within 30 s, it is finalized as "no response" with retry.
- Local intent fast path: sub-50ms responses bypassing agent
- Busy/dropped handling: shows "busy — try again"
- Markdown rendering: bold, italic, inline code (amber-tinted), code blocks (monospace), `[label](url)` links, bare URLs (mangled http/https schemes like `hthtps://` from upstream limit banners are repaired before linkifying; unknown schemes stay plain text), ordered/unordered lists, and tables (styled header + zebra rows). Agent bubbles get full markdown; user bubbles stay verbatim except URLs, which are linkified with the same scheme repair

**Empty State & Suggestions**
- When a conversation has no messages, the chat area shows a large breathing assistant orb, a localized title/subtitle, and four clickable **suggestion chips**. Clicking a chip fills the composer (does not auto-send) so the user can edit first.

**Localization (i18n)**
- The chat's own UI strings (empty-state title/subtitle, suggestion chips, the top-bar "thinking"/"online" status) are localized via `src/lib/i18n.ts` — a lightweight hand-rolled module mirroring the Go backend's `system/lib/i18n` conventions (canonical codes `en` / `vi` / `zh-CN` / `zh-TW`, alias normalization, **English fallback** per key).
- The active language is resolved from the device config's `stt_language` field (the same source Go's `i18n.Lang()` reads from `config.STTLanguage`) via `setLanguage()` in `App.tsx` on first config load, with the Chat section re-applying it from its own config fetch. Components read strings through the `useT()` hook, which re-renders when the language resolves.
- This i18n module currently covers only the chat strings added with the redesign; the rest of the Monitor UI remains hardcoded English.

**Data Flow**
```
Chat UI → POST /api/sensing/event → SensingHandler
  → openclaw.SendChatMessage() → WebSocket chat.send → OpenClaw
  → Response streams via WebSocket (thinking → assistant deltas → lifecycle end)
  → SSE /api/agent/flow-stream → Chat UI updates message in real-time
```

---

## 6. LED Color API

### Problem
Original `GET /hw/led` only returned `{ led_count: 64 }` — no current color info.

### Solution
Added `GET /hw/led/color` to `hal/server.py`:

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

`system/web/src/components/SourceFooter.tsx` is a tiny `position: fixed` link mounted at the App root (`App.tsx`, outside `<Routes>`), so it appears on every page — Setup, Login, Monitor, GwConfig.

Renders at `bottom: 6px, right: 8px` with monospace 10px text and opacity `0.7` — visible to anyone who looks for it without blocking form action buttons (Back / Next / Setup / Save) or scroll. Link target: `https://github.com/autonomous-ai/autonomous-os`.

Reason it exists: HAL Python (`hal/`) ships GPL v3, baked into the board image. GPL §6 requires recipients of the binary to be informed where corresponding source lives. The footer satisfies the "written offer" alternative by exposing the public repo URL on the device itself. See also `scripts/release/tag-release.sh` + `Makefile:tag-release` for the version → commit traceability piece.

---

## 9. Build & Deploy

```bash
# Build production
make web-build        # tsc + vite build → system/web/dist/

# Deploy to one device by IP (dev push — NOT the OTA fleet path)
IP=172.168.20.255 make device-deploy   # hal + os-server
IP=172.168.20.255 make hal-deploy      # hal only, no build step
IP=172.168.20.255 make os-deploy       # cross-compile + swap the binary
```

Backed by `scripts/deploy-device.sh`. `PI_USER` defaults to `orangepi` and
`PI_PASS` to `orangepi` (needs `sshpass`); set `PI_PASS=""` to use your SSH key
and interactive sudo instead. `PI_HOST` works in place of `IP`.

`.env`, `.venv` and `calibration/` on the device are never overwritten, and the
swap runs without `--delete`, so device-local paths outside the repo survive.

> **Run `--dry-run` first when your branch might be behind the device.** The
> swap overwrites, so a stale checkout can silently revert work that only
> exists on the device:
> `IP=<DEVICE_IP> bash scripts/deploy-device.sh --hal --dry-run`

These are for a single device on your LAN. To ship to the fleet, use the OTA
path instead — `make upload-hal` then `make promote-hal`, which versions the
artifact and rolls it out.
