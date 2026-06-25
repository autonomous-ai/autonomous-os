# Claude Desktop Buddy — Integration Spec

> Turns the Lamp lamp into a Hardware Buddy for Claude Desktop. Runs as a
> standalone Go plugin on the Pi and bridges Claude's BLE state into the
> existing HAL (LED/audio) and Lamp (OpenClaw/sensing) stacks.

**Source**: [anthropics/claude-desktop-buddy](https://github.com/anthropics/claude-desktop-buddy) (ESP32 reference firmware + protocol REFERENCE.md)
**Status**: Implementation — Phase 1, 2, 3 shipped (2026-05-11)
**Hardware target**: Raspberry Pi 4 / Orange Pi 4 Pro (AIC8820 BT chip)

---

## 1. What & Why

Claude Desktop ("Claude for macOS/Windows") exposes a BLE API under
Developer Mode that lets a hardware companion connect over Nordic UART
Service. The Anthropic reference is an ESP32 desk pet — small LCD, two
buttons, no brain. Lamp implements the same wire protocol but as a
**smart buddy**: full lamp with camera, mic, speaker, LED ring, servo,
and the OpenClaw agentic brain. (No LCD/display peripheral today.)

Lamp can reflect Claude's state visually, voice-approve tool calls
hands-free, stream chat turns to its display/TTS, and feed presence
context back.

### Use cases

| # | Use case | Status | Description |
|---|----------|--------|-------------|
| UC-1 | **Ambient state** | [x] shipped | LED ring reflects Claude state (sleep/idle/busy/attention/heart/celebrate). The current Lamp lamp has no LCD/display, so only the LED is driven. |
| UC-2 | **Voice approval** | [x] shipped | Tool-call prompt → Lamp speaks it via OpenClaw skill → user says approve/deny hands-free. |
| UC-3 | **Activity stats over HTTP** | [x] shipped | Buddy tracks token count, sessions running, and approval stats; exposed via `GET /status` for any local consumer. (No on-lamp display today.) |
| UC-4 | **Chat-turn fan-out** | [x] shipped | Every `evt:"turn"` (user / assistant / tool blocks) is forwarded to Lamp monitor bus as `buddy_event` — ready for TTS, transcript memory, dashboard. |
| UC-5 | **Character pack receive** | [x] shipped | Desktop can drag a GIF folder onto its panel → streams over BLE → saved to `/opt/claude-desktop-buddy/chars/<name>/`. |
| UC-9 | **Activity TTS narration** | [x] shipped | Short status announcements on state transitions ("Claude connected" / "Claude is starting" / "Claude is done" / "Claude disconnected") and per `tool_use` / `thinking` block ("Claude is editing a file", "Claude is searching the web", …). Multi-language (`vi` / `en` / `zh`) via `i18n.go`, throttled once-per-turn-per-category. Sent to HAL `/voice/speak` with `cached: true` so the bounded phrase set hits the on-disk TTS cache after first play; `Narrator.Warmup` fires every phrase through `prerender: true` 8s after startup so the very first announcement also plays from cache. The busy→idle "done" transition additionally calls `/emotion {happy,0.7}` so HAL coordinates a quick LED + servo "exhale" between turns. Unknown tool names fall back to a name-less generic phrase — Claude Code's CamelCase / `mcp__*` names don't sound like words through TTS. |
| UC-8 | **Voice readout of Claude reply** | [ ] next | Lamp subscribes to `buddy_event`, filters `role=assistant` + text blocks, strips markdown, and pipes the text to HAL TTS so the user can listen instead of looking at the Mac. Respects presence (skip when user is away), voice-pipeline busy state, and agent emotion priority. |
| UC-6 | **Presence feedback** | [ ] future | Lamp presence (camera/PIR) → Desktop. Requires protocol extension. |
| UC-7 | **Transcript-aware OpenClaw** | [ ] future | OpenClaw reads buffered chat history when user asks via voice. |

---

## 2. Architecture

```
┌──────────────────┐       BLE (Nordic UART)        ┌──────────────────────────────┐
│  Claude Desktop  │ ◄───────────────────────────►  │          Lamp (Pi)           │
│  (Mac / Windows) │                                │                              │
│  Developer →     │   Heartbeat (msg/running/      │  ┌────────────────────────┐  │
│  Hardware Buddy  │   tokens/prompt), Event        │  │   buddy-plugin         │  │
│                  │   (turn/content blocks),       │  │   (Go, :5002)          │  │
│                  │   Command, TimeSync            │  │                        │  │
│                  │                                │  │   ble.go + agent.go    │  │
│                  │   Ack, PermissionDecision      │  │   protocol.go          │  │
│                  │                                │  │   state.go             │  │
│                  │                                │  │   bridge.go            │  │
│                  │                                │  │   httpapi/             │  │
│                  │                                │  │   transfer.go          │  │
│                  │                                │  └──┬──────────┬──────────┘  │
│                  │                                │     │ HTTP     │ HTTP        │
│                  │                                │     ▼          ▼             │
│                  │                                │  ┌─────────┐ ┌──────────┐    │
│                  │                                │  │  Lamp   │ │ HAL   │    │
│                  │                                │  │ :5000   │ │ :5001    │    │
│                  │                                │  │ OpenClaw│ │ LED ring │    │
│                  │                                │  │ sensing │ │ + TTS    │    │
│                  │                                │  │ monitor │ │ (no LCD) │    │
│                  │                                │  └─────────┘ └──────────┘    │
└──────────────────┘                                └──────────────────────────────┘
```

### Plugin layout

```
claude-desktop-buddy/
├── main.go              Thin entrypoint: flags + logging, then buddy.Run()
├── buddy/               Daemon logic (package buddy)
│   ├── app.go           Run() orchestration, config load, BLE message dispatch
│   ├── ble.go           BLE peripheral (GATT server, advertising, debugfs interval tune)
│   ├── agent.go         BlueZ DisplayOnly pairing agent (registered but unused — see §5)
│   ├── protocol.go      Wire types: Heartbeat, TimeSync, Event, Command, Ack, PermissionDecision
│   ├── state.go         6-state machine (sleep/idle/busy/attention/heart/celebrate)
│   ├── bridge.go        HTTP outbound to HAL (:5001) + Lamp (:5000)
│   ├── transfer.go      Character-pack folder push receiver (saves under chars/)
│   ├── narrator.go,i18n.go  Per-turn-deduped TTS narration (EN/VI)
│   ├── stats.go         Lifetime approve/deny counters (persisted under /var/lib)
│   └── approval_service.go, status_provider.go, activity_sink.go   Impls of the httpapi ports
├── httpapi/             HTTP API :5002 delivery layer (package httpapi)
│   ├── server.go        Server + routes() registry + JSON helpers
│   ├── ports.go         Interfaces: StatusProvider, ApprovalService, ActivitySink
│   ├── status.go        GET /status, GET /health
│   ├── claudedesktop.go POST /claude-desktop/approve, /deny
│   └── claudecode.go    POST /claude-code/notify, /usage (Claude Code plugin pushes)
├── claude-code-buddy/   Claude Code plugin (Python) — pushes to /claude-code/* (see its own README)
├── skill/SKILL.md       OpenClaw skill descriptor for voice approval flow
├── config/buddy.json    Template config (runtime reads /root/config/buddy.json)
├── third_party/bluetooth/  Vendored tinygo bluetooth v0.14.0 + secure-* flag patch
├── go.mod               Separate module with replace tinygo.org/x/bluetooth => ./third_party/...
└── VERSION_BUDDY        Plain-text version stamp injected at build time
```

### Process model

`buddy-plugin` is a **standalone systemd service** (`claude-desktop-buddy.service`)
separate from the main Lamp binary. Restarts independently; never linked
into the Lamp process. Talks to Lamp and HAL purely over local HTTP.

```
Pi runtime layout:
  /opt/claude-desktop-buddy/buddy-plugin   — binary
  /opt/claude-desktop-buddy/VERSION_BUDDY  — version stamp
  /opt/claude-desktop-buddy/chars/         — received character packs
  /root/config/buddy.json                  — runtime config (created once)
  /etc/systemd/system/claude-desktop-buddy.service   — service unit
  /var/log/claude-desktop-buddy.log                  — rotated log (2MB × 10)
```

---

## 3. Discovery and pairing — what actually happens today

The Anthropic spec recommends LE Secure Connections bonding with
DisplayOnly IO capability. In practice Claude Desktop's current macOS
client establishes a plain LE connection without auto-triggering SMP,
so encrypted-only characteristics are inaccessible and the panel
reports "No response". We therefore **expose the NUS characteristics
without encryption flags** and the connection runs unbonded. The vendor
fork that adds BlueZ `secure-read`/`secure-write` flags stays in tree,
and `agent.go` still registers a DisplayOnly agent, so encryption can be
flipped back on once the desktop side starts driving SMP.

### Discovery filter (Hardware Buddy panel)

The Hardware Buddy device picker filters to:

1. Device name starting with **`Claude`**
2. Advertising the **Nordic UART Service UUID** (`6e400001-…`)

Anything else is hidden in the picker even if macOS Bluetooth Settings
can see it.

### Pairing flow (Pi side)

1. BlueZ runtime config (must be in place — `setup-claude-desktop-buddy.sh`
   sets these; manual fallback: `btmgmt -i 0 power off; bredr on; le on;
   connectable on; pairable on; discoverable on; power on` + `bluetoothctl
   discoverable-timeout 0`).
2. buddy-plugin starts → reads `/root/config/buddy.json` → resolves device
   name via `resolveDeviceName()`:
   - Reads `device_name` from config (default `Claude-{deviceid}`).
   - If it contains `{deviceid}`, fetch `device_id` from Lamp
     `GET http://127.0.0.1:5000/api/system/info` (retries up to 15 × 2s).
   - `shortDeviceID()` keeps only the trailing dash-segment, truncated to
     4 chars (`lamp-004` → `004`) so name + Nordic UART UUID both fit in
     the 31-byte primary advertisement payload.
   - Falls back to `Claude-unknown` if the device hasn't been
     provisioned yet via `/api/device/setup`.
3. `registerBluezAgent()` exports an `org.bluez.Agent1` with
   `DisplayOnly` capability on the system D-Bus, then calls
   `RegisterAgent` + `RequestDefaultAgent` on `org.bluez.AgentManager1`.
   The agent currently only logs `PAIRING PASSKEY` events when BlueZ
   asks for them — useful if SMP is ever engaged.
4. `tuneAdvIntervals()` writes `160` and `320` to
   `/sys/kernel/debug/bluetooth/hci*/adv_{min,max}_interval` (units of
   0.625 ms, so 100–200 ms). Without this BlueZ defaults to 1.28 s,
   which is too sparse for macOS's short scan windows.
5. tinygo registers the GATT service + advertisement. BlueZ packs the
   service UUID in the primary advertisement (18 bytes) and the local
   name in the scan response (~10 bytes). macOS active-scans, merges
   both, and surfaces the device in the picker.

### Pairing flow (Mac side)

1. User: **Help → Troubleshooting → Enable Developer Mode** (one-time).
2. **Developer → Open Hardware Buddy…** → click **Connect** → pick
   `Claude-XXX` from scan results.
3. Hardware Buddy opens an LE GATT connection. macOS does *not*
   initiate SMP because our characteristics don't request encryption.
4. Desktop immediately sends `{"cmd":"owner","name":"…"}`, a TimeSync,
   and starts polling `{"cmd":"status"}` ~every 2 s.
5. Once an agentic turn begins, `Heartbeat` snapshots and `Event` chat
   turns start flowing.

### Auto-reconnect

The Mac caches the device by BD address. Subsequent buddy starts pair
back automatically without user action. **Renaming the device on the Pi
does not refresh the Mac-side cached name** — Mac keeps the old label
until the user explicitly forgets the device in System Settings →
Bluetooth, or runs `sudo pkill bluetoothd`. On stubborn caches a full
plist wipe works (`sudo rm /Library/Preferences/com.apple.Bluetooth*.plist`
+ pkill).

### Unpair

`{"cmd":"unpair"}` from Desktop → buddy aborts any in-progress folder
transfer, drops back to advertising. BlueZ-side bond data is not
cleared because there is no bond.

---

## 4. BLE wire protocol

### Transport

| Property | Value |
|----------|-------|
| Service UUID | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` |
| RX (Desktop → Device, write + write-without-response) | `6e400002-b5a3-f393-e0a9-e50e24dcca9e` |
| TX (Device → Desktop, notify + read) | `6e400003-b5a3-f393-e0a9-e50e24dcca9e` |
| Wire format | UTF-8 JSON, one object per `\n`-terminated line |
| Device name | Must start with `Claude` |
| Advertising interval | 100–200 ms (tuned via debugfs) |
| Encryption | None today; vendor fork ready to re-enable via `secure-*` flags |

### Messages: Desktop → Device

#### `Heartbeat` — periodic state snapshot

Sent ~every 1 s when active, ~10 s when idle. Parser uses presence of
the `total` field as the discriminator.

```json
{
  "total": 3,
  "running": 1,
  "waiting": 0,
  "msg": "Editing main.go",
  "entries": ["Latest message", "Previous message"],
  "tokens": 52340,
  "tokens_today": 8200,
  "prompt": { "id": "req_abc123", "tool": "Edit", "hint": "server.go:10-20" }
}
```

Buddy throttles its log line: `[ble] heartbeat …` is only emitted when
`running`, `waiting`, `msg`, or `prompt` (presence / id) changes —
token counts drift on every ping and would otherwise spam the journal.

#### `TimeSync` — clock + timezone

Sent once on connect (presence of `time` field).

```json
{ "time": [1713600000, 25200] }
```

#### `Event` — chat turn stream

Sent for each conversational turn (user input, assistant reply, tool
use, tool result). Presence of `evt` field. `content` is either a bare
string (user turns) or an array of typed content blocks (assistant +
tool flows).

```json
{
  "evt": "turn",
  "role": "user",
  "content": "thử tìm giá BTC hôm nay đi"
}
```

```json
{
  "evt": "turn",
  "role": "assistant",
  "content": [
    {"type": "thinking", "thinking": "", "signature": "…base64…"},
    {"type": "tool_use", "id": "toolu_…", "name": "WebSearch",
     "input": {"query": "Bitcoin price today"}}
  ]
}
```

`formatContentBlock()` renders each block into a single-line log tag:
`[thinking: …]`, `[tool_use <name>(<input>)]`, `[tool_result <id>: …]`,
`[tool_ref: <name>]`. Each event is fanned out to Lamp via
`bridge.OnEvent` as `type=buddy_event` on the monitor bus.

#### `Command` — control + folder push

Presence of `cmd` field. Buddy acks every command.

| `cmd` | Payload | Effect |
|---|---|---|
| `status` | `{}` | Buddy replies with `Ack` carrying battery / uptime / approval counts. |
| `owner` | `{"name": "Leo"}` | Records the Mac user name for logging. |
| `name` | `{"name": "…"}` | Renames the device (cosmetic). |
| `unpair` | `{}` | Drops back to advertising. |
| `char_begin` | `{"name":"bufo","total":1500000}` | Starts a character-pack folder transfer. |
| `file` | `{"path":"sleep.gif","size":12345}` | Opens a file in the active transfer. |
| `chunk` | `{"d":"<base64>"}` | Appends decoded bytes to the open file. |
| `file_end` | `{}` | Closes the current file. |
| `char_end` | `{}` | Closes the transfer; folder is now under `chars/<name>/`. |

### Messages: Device → Desktop

#### `Ack` — response to commands

```json
{ "ack": "owner", "ok": true, "n": 0 }
```

Status ack carries a `data` payload:

```json
{
  "ack": "status",
  "ok": true,
  "data": {
    "name": "Claude-004",
    "sec": false,
    "bat": { "pct": 100, "mV": 5000, "mA": 0, "usb": true },
    "sys": { "up": 86400, "heap": 0 },
    "stats": { "appr": 3, "deny": 0, "vel": 0, "nap": 0, "lvl": 0 }
  }
}
```

`sec` is currently hardcoded `false` (no bonding). Set to `true` once
encryption is enabled.

#### `PermissionDecision` — approve / deny

Sent in response to a heartbeat `prompt`. `id` echoes back the prompt id.

```json
{ "cmd": "permission", "id": "req_abc123", "decision": "once" }
```

| Decision | Effect |
|---|---|
| `"once"` | Approves the pending tool call. |
| `"deny"` | Rejects it. |

### Salvage for write-without-response packet loss

Claude Desktop streams chunks via BLE write-without-response, which has
no ATT confirmation. When BlueZ silently drops a chunk, the next line we
extract has corrupted brackets. `ParseOrSalvage` looks for the latest
known JSON opener (`{"cmd":"`, `{"time":`, `{"total":`, `{"evt":"`) in
the buffer and retries. If nothing parses, the line is dropped with one
of three category logs:

- `dropped N-byte BLE message (prefix-lost): …` — head of the line was
  lost; nothing to salvage.
- `dropped N-byte BLE message (truncated): …` — line starts JSON but
  has no closing `}`.
- `dropped N-byte BLE message (mid-corruption): …` — brackets line up
  but a chunk inside an `entries` / `content` array vanished.

All three abort any in-progress folder transfer because framing is lost.

---

## 5. Encryption status (deferred)

Per the Anthropic spec, NUS characteristics should require LE Secure
Connections bonding with the device exposing DisplayOnly IO capability,
and `sec: true` should be reported once bonded. The vendor fork at
`third_party/bluetooth/` adds the BlueZ `secure-read` and `secure-write`
flags to `tinygo.org/x/bluetooth`'s six-bit `CharacteristicPermissions`
enum so we can do this in Go.

The blocker: the current macOS Hardware Buddy client connects without
issuing an SMP pairing request, so when our characteristics were marked
secure-only BlueZ rejected every GATT operation and the panel sat on
"No response". To make the integration work end-to-end today we ship the
characteristics with plain `write` / `write-without-response` / `notify`
/ `read` flags and report `sec: false`. The agent + flag plumbing stays
ready so we can flip back to encrypted-only once Anthropic enables
auto-pairing on their side, or we find an explicit "Pair" affordance to
drive.

---

## 6. State machine

```
                    ┌─────────────┐
   BLE off ────────►│    sleep    │◄──────── BLE disconnected
                    │ LED: off    │
                    └──────┬──────┘
                           │ connect
                           ▼
                    ┌─────────────┐
   running == 0    │    idle     │
   ───────────────►│ ambient-led │
                    └──────┬──────┘
                           │ running > 0
                           ▼
                    ┌─────────────┐
   waiting == 0    │    busy     │
   ───────────────►│ LED: pulse  │
                    └──────┬──────┘
                           │ heartbeat.prompt != null
                           ▼
                    ┌─────────────┐
                    │  attention  │
                    │ LED: blink  │
                    │ + sensing   │
                    └──────┬──────┘
                           │ /claude-desktop/approve or /deny
                           ▼
                    ┌─────────────┐
                    │   heart     │  approved < 5 s
                    │ LED: warm   │  (3 s, then re-derive)
                    └─────────────┘

      tokens crossing 50K boundary:
                    ┌─────────────┐
                    │  celebrate  │  rainbow burst
                    │ LED: rainbow│  (3 s, then re-derive)
                    └─────────────┘
```

### Derivation rules

```
disconnected           → sleep
heartbeat.prompt != nil → attention
heartbeat.running > 0  → busy
otherwise              → idle

approve within 5 s of prompt arrival → heart   (3 s overlay)
tokens / 50_000 increments             → celebrate (3 s overlay)
```

Transient overlays (`heart`, `celebrate`) lock state for 3 s; an
expiry ticker (`CheckTransientExpiry`, 500 ms) re-derives from the last
heartbeat once the lock elapses.

---

## 7. State → HAL + Lamp bridge

`Bridge.OnStateChange` is wired as the state machine's transition
callback. Each transition fires:

| State | HAL LED call | Lamp monitor event |
|---|---|---|
| `sleep` | `POST /led/off` | `buddy_state` |
| `idle` | (none — ambient owns LED) | `buddy_state` |
| `busy` | `/led/effect {pulse,[0,100,255],0.8}` | `buddy_state` |
| `attention` | `/led/effect {blink,[255,80,0],1.5}` | `buddy_state` + **`buddy_approval` sensing event** |
| `heart` | `/led/solid {[255,200,100]}` | `buddy_state` |
| `celebrate` | `/led/effect {rainbow,*,2.0,3000ms}` | `buddy_state` |

> The current Lamp lamp has no LCD/eye display; the `bridge.go` code
> still attempts `/display/info`, `/display/eyes`, and `/display/eyes-mode`
> calls on HAL, but they're no-ops on hardware without a screen.
> Either remove those branches when the no-display constraint is
> permanent, or keep them and add the display peripheral.

Additionally, every `Event` (chat turn) is forwarded via `Bridge.OnEvent`:

```
POST http://127.0.0.1:5000/api/monitor/event
{
  "type": "buddy_event",
  "summary": "buddy turn assistant",
  "detail": { "evt": "turn", "role": "assistant", "content": "<rendered text>" }
}
```

Downstream Lamp consumers can subscribe to `buddy_event` for TTS,
transcript memory, dashboard, etc. — none of those are wired yet.

### LED priority placement

The system has a four-level LED hierarchy. Buddy fits at level 1.5:

```
Level 0   Status LED   (error, OTA, booting, connectivity, listening)
Level 1   Agent emotion (OpenClaw [HW:/emotion:…])
Level 1.5 Buddy state  ← here
Level 2   Local intent (voice "bật đèn xanh")
Level 3   Ambient breathing
```

Ambient service listens on the monitor bus for `buddy_state`:

| Buddy state | Ambient reaction |
|---|---|
| `attention`, `busy` | Treat as `led_set` — pause ambient breathing. |
| `idle`, `sleep` | Treat as `led_off` — resume ambient. |
| `heart`, `celebrate` | Transient, auto-unlock after 3 s. |

Agent emotion still wins over buddy; user voice intents win over both.

---

## 8. Voice approval flow (UC-2)

```
1. Heartbeat arrives with prompt != null
2. state → attention; bridge fires
     HAL: blink orange + display "Approve <tool>?"
     Lamp:   POST /api/sensing/event { type:"buddy_approval", message:"Claude Desktop needs approval: …" }
3. OpenClaw routes the sensing event to skill `claude-desktop-buddy`
4. Skill (SKILL.md in claude-desktop-buddy/skill/) does:
     - Express emotion: curious 0.8
     - Speak the prompt naturally over TTS
     - Wait for verbal "yes/approve/ok" or "no/deny/skip"
5. Skill curls back:
     POST http://127.0.0.1:5002/claude-desktop/approve  {"id":"req_abc123"}   (or /claude-desktop/deny)
6. buddy-plugin returns BLE PermissionDecision:
     {"cmd":"permission","id":"req_abc123","decision":"once" | "deny"}
7. Desktop unblocks. State → heart (if user replied in <5 s) → busy → idle.
```

Why route through OpenClaw rather than answering in buddy directly:

- OpenClaw owns TTS (markdown stripping, voice character, queue logic).
- OpenClaw already coordinates with the busy/listening state of the
  voice pipeline so the approval question doesn't talk over an
  in-progress conversation.
- OpenClaw can choose to *not* speak when the user is away (presence)
  — leaving just the LED blink.
- Events land in the existing flow-events log for monitoring.

### Skill location

```
claude-desktop-buddy/skill/SKILL.md
```

The skill ships with the buddy binary and is **not** copied into
`lamp/resources/openclaw-skills/`. OpenClaw picks it up from the buddy
install dir at runtime (see SKILL.md for the exact discovery rule).

---

## 9. HTTP API (port 5002)

| Method | Path | Description | Body / Response |
|---|---|---|---|
| `GET` | `/health` | Liveness | `{status, ble_advertising, uptime_seconds}` |
| `GET` | `/status` | Current buddy state | See below |
| `POST` | `/claude-desktop/approve` | Approve the pending prompt | Body `{id}`. Returns `{ok}`. |
| `POST` | `/claude-desktop/deny` | Deny the pending prompt | Body `{id}`. Returns `{ok}`. |
| `POST` | `/claude-code/notify` | Claude Code activity push (plugin) | Body `{title,subtitle,level,sound}`. Logs; returns `{ok}`. HAL bridge pending. |
| `POST` | `/claude-code/usage` | Claude Code usage push (plugin) | Body `{five_hour,seven_day,reset_5h,reset_7d,sound}`. Logs; returns `{ok}`. |

`/status` response:

```json
{
  "state": "attention",
  "connected": true,
  "sessions_running": 1,
  "tokens_today": 8200,
  "pending_prompt": {
    "id": "req_abc123",
    "tool": "Edit",
    "hint": "server.go lines 10-20",
    "received_at": ""
  }
}
```

`/claude-desktop/approve` and `/claude-desktop/deny` reject with `409 Conflict` if there is no pending
prompt or the `id` doesn't match the current one — prevents the OpenClaw
skill from acting on stale prompts.

---

## 10. Lamp-side coordination

### Config-change watcher (os/services/server/server.go)

When the user provisions the device via `POST /api/device/setup`, Lamp
saves `device_id` to `config/config.json` and notifies the in-process
config bus. The Lamp server listens on that bus and, when the
`device_id` value transitions, runs:

```
systemctl cat claude-desktop-buddy.service   # skip silently if not installed
systemctl restart claude-desktop-buddy
```

That gives buddy a chance to re-resolve `Claude-{deviceid}` to the
freshly assigned id without manual intervention. Lamps that don't ship
the buddy plugin are no-op'd by the `systemctl cat` pre-check.

### Lamp system info endpoint

Buddy reads `device_id` over HTTP rather than the config file directly:

```
GET http://127.0.0.1:5000/api/system/info
→ { "data": { "deviceId": "lamp-004", … } }
```

This keeps buddy oblivious to Lamp's config schema.

---

## 11. Config

`/root/config/buddy.json` (created once by `setup-claude-desktop-buddy.sh`
or `software-update claude-desktop-buddy`, never overwritten by tooling
afterwards):

```json
{
  "enabled": true,
  "device_name": "Claude-{deviceid}",
  "http_port": 5002,
  "lelamp_url": "http://127.0.0.1:5001",
  "lamp_url": "http://127.0.0.1:5000",
  "approval_timeout_sec": 30,
  "led_mapping": {
    "sleep":     { "action": "off" },
    "idle":      { "action": "none" },
    "busy":      { "effect": "pulse",   "color": [0, 100, 255],   "speed": 0.8 },
    "attention": { "effect": "blink",   "color": [255, 80, 0],    "speed": 1.5 },
    "heart":     { "action": "solid",   "color": [255, 200, 100], "duration_ms": 3000 },
    "celebrate": { "effect": "rainbow", "speed": 2.0, "duration_ms": 3000 }
  }
}
```

`led_mapping` is parsed and forwarded to HAL untouched; the template
mirrors what `bridge.OnStateChange` currently does in code. `idle` is
`none` so the ambient service keeps control of the LED when the lamp
isn't actively reflecting Claude state.

---

## 12. BLE library: vendored tinygo

We started on upstream `tinygo.org/x/bluetooth v0.14.0`. Two needed
features were either TODOs (`MinInterval` / `MaxInterval` for
advertising) or simply missing (BlueZ `secure-read` / `secure-write`
characteristic flags). We vendored the lib into
`third_party/bluetooth/` and added them. `go.mod` carries:

```
replace tinygo.org/x/bluetooth => ./third_party/bluetooth
```

Patches:

- `gatts.go` — extended `CharacteristicPermissions` from 6 bits to 8
  with `CharacteristicSecureReadPermission` and
  `CharacteristicSecureWritePermission`.
- `gatts_linux.go` — added `"secure-read"` and `"secure-write"` to the
  BlueZ flag-string array so the new bits map onto the D-Bus
  representation.

The advertising interval is set via the kernel debugfs route
(`/sys/kernel/debug/bluetooth/hci*/adv_{min,max}_interval`) rather than
patching tinygo — see `tuneAdvIntervals()` in `ble.go`. This works on
any BlueZ regardless of whether the D-Bus `MinInterval`/`MaxInterval`
properties are honoured.

---

## 13. Chip / kernel notes (Orange Pi 4 Pro, AIC8820)

The Orange Pi 4 Pro ships with an AIC8820 BT chip (Aicsemi, manufacturer
ID 2875), UART-attached. Quirks we hit:

- **No factory MAC**: `bdaddr` is `10:11:12:13:14:15` out of the box.
  `btmgmt -i 0 public-addr <new>` returns `0x0c Not Supported`.
- **No LE Privacy**: `bluetoothd` logs `Failed to set privacy: Rejected
  (0x0b)` at startup. Cosmetic; LE advertising still works.
- **Reset hazard**: repeated `bluetoothd` / chip restarts can wedge the
  controller into a state where it `UP RUNNING PSCAN` (classic only) and
  refuses LE advertising for a bit. Recovery: `sudo systemctl restart
  bluetooth && sudo hciconfig hci0 reset && sudo systemctl restart
  claude-desktop-buddy`.

Raspberry Pi (Broadcom / RP1) does not have these quirks and uses its
factory MAC, but the rest of the integration is identical.

---

## 14. Deployment

### systemd unit (`/etc/systemd/system/claude-desktop-buddy.service`)

```ini
[Unit]
Description=Lamp Claude Desktop Buddy
After=bluetooth.target os-server.service
Wants=bluetooth.target

[Service]
ExecStart=/opt/claude-desktop-buddy/buddy-plugin -config /root/config/buddy.json -log /var/log/claude-desktop-buddy.log
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
```

### Install paths

| Path | Purpose |
|---|---|
| `/opt/claude-desktop-buddy/buddy-plugin` | Binary (`linux/arm64`) |
| `/opt/claude-desktop-buddy/VERSION_BUDDY` | Version stamp matching OTA metadata |
| `/opt/claude-desktop-buddy/chars/<name>/` | Character packs from folder pushes |
| `/root/config/buddy.json` | Runtime config (preserved across OTA) |
| `/var/lib/claude-desktop-buddy/stats.json` | Lifetime approval / denial counters (preserved across OTA + config reset) |
| `/var/log/claude-desktop-buddy.log` | Rotated log (2 MB × 10 backups) |

### Update commands

- First install: `setup-claude-desktop-buddy.sh` (downloads from OTA
  metadata, creates service, leaves config alone if it already exists).
- Subsequent updates: `software-update claude-desktop-buddy` (binary + version
  stamp + service restart only; config is never overwritten).

---

## 15. Risks & known gaps

| Risk / gap | Impact | Status |
|---|---|---|
| Connection is unencrypted (`sec: false`) | Panel shows "Connection is unencrypted" warning; GATT data in clear | Accepted until Desktop side drives SMP |
| Mac BT cache by BD address | Renames on Pi don't refresh Mac-visible name | Forget device on Mac after rename |
| AIC8820 fake MAC | Multiple AIC8820 lamps on the same Mac collide in cache | Out of scope until vendor MAC support lands |
| BLE write-without-response packet loss | Long heartbeats / events get mid-corrupted | `ParseOrSalvage` recovers when possible; drops with a category log otherwise |
| LED conflict with agent emotion | Buddy + emotion both touch LED | Buddy sits below emotion via monitor-bus priority |
| Voice approval collides with active TTS | Approval question talks over conversation | OpenClaw skill routes through TTS queue, respects busy state |

---

## 16. Success criteria — current state

- [x] Claude Desktop sees `Claude-XXX` in the Hardware Buddy picker
- [x] Pairing succeeds, auto-reconnects after Mac wake / Pi reboot
- [x] LED reflects state without overwriting agent emotion or ambient
- [x] Voice approve / deny routes through OpenClaw and completes end-to-end
- [x] Token count + running sessions tracked + exposed via `/status` (no on-lamp display yet)
- [x] Buddy crash does not affect main Lamp server
- [x] OpenClaw reduces proactive behaviour when Desktop is busy
- [x] Chat turns (user / assistant / tool blocks) stream into Lamp monitor bus
- [x] Character pack folder push lands under `chars/<name>/`
- [x] UC-9 activity TTS narration (vi/en/zh) routes through HAL cache + emotion on done
- [x] Approval / denial counters persist across restart (`/var/lib/claude-desktop-buddy/stats.json`)
- [ ] UC-8 voice readout of assistant reply — next
- [ ] Encrypted bonded GATT link (`sec: true`) — deferred
- [ ] Presence feedback Lamp → Desktop — future protocol extension
- [ ] Transcript context injection into OpenClaw — future
