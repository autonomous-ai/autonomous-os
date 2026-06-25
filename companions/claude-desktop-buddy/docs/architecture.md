# Architecture

Claude Desktop Buddy is a single Go binary (`buddy-plugin`) that runs on the
device and bridges **Claude Desktop on the user's Mac** to the lamp's hardware
runtime. This document describes its components, how data flows through them, and
the contracts it speaks on each side.

For the on-the-wire BLE message format see [`ble-protocol.md`](ble-protocol.md).
For building, deploying, config, and pairing see [`setup.md`](setup.md).

## The two sides

```
   Mac                                    Device (Pi / OrangePi)
 ┌───────────────────┐               ┌───────────────────────────────────────┐
 │  Claude Desktop    │   BLE / NUS   │  buddy-plugin                          │
 │  "Hardware Buddy"  │ ◄───────────► │                                        │
 └───────────────────┘  notify / write│  ble.go ─► state.go ─► bridge.go       │
                                       │     │                      │  │  │     │
                                       │     ▼                      ▼  ▼  ▼     │
                                       │  httpapi/ (:5002)     LeLamp / Lamp    │
                                       │  (OpenClaw)           HTTP (LED, eyes, │
                                       │                       TTS, monitor)    │
                                       └───────────────────────────────────────┘
```

- **North (BLE):** Claude Desktop is the BLE *central*; the device is the
  *peripheral* advertising a Nordic UART Service. Desktop streams heartbeats,
  chat events, permission prompts, and folder data; the device replies with
  acks and permission decisions.
- **South (HTTP):** the device's behavior is produced by calling two local HTTP
  servers — **LeLamp** (the hardware runtime, default `:5001`) for LED / display
  / emotion / TTS, and **Lamp** (the Go API, default `:5000`) for the monitor and
  sensing buses.
- **East (HTTP):** Buddy itself serves a small HTTP API on `:5002` that the
  on-device agent (OpenClaw skill) calls to read status and approve/deny prompts.

## Components (by file)

| File | Responsibility |
|------|----------------|
| `main.go` | Process entry. Loads config, registers the BlueZ pairing agent, wires the state machine → bridge + narrator, starts the BLE server, the transient-state ticker, and the HTTP server. Owns the BLE message dispatcher `handleBLEMessage`. |
| `ble.go` | Nordic UART GATT server (via a vendored `tinygo.org/x/bluetooth` fork). Advertising, line framing, salvage, chunked notify writes, advertising-interval tuning. |
| `protocol.go` | All wire types (`Heartbeat`, `TimeSync`, `Event`, `Command`, `Ack`, `PermissionDecision`), the parser/salvager, and ack/permission builders. |
| `state.go` | `StateMachine` — derives a `BuddyState` from heartbeats, tracks the pending prompt and lifetime approval/denial counters, and manages transient states. |
| `bridge.go` | Maps each state change to concrete LeLamp + Lamp HTTP calls (LED, display, emotion, TTS, monitor/sensing events). |
| `httpapi/` | The `:5002` API delivery package (clean architecture / dependency inversion). `server.go` holds the `Server`, the single `routes()` registry, and JSON response helpers; `ports.go` declares the interfaces the handlers depend on (`StatusProvider`, `ApprovalService`, `ActivitySink`, plus sentinel errors); `status.go` serves `GET /status` + `GET /health`; `claudedesktop.go` serves `POST /claude-desktop/approve` + `/deny` (the Claude Desktop voice-approval round-trip); `claudecode.go` serves `POST /claude-code/notify` + `/usage` (the Claude Code plugin pushes). |
| `status_provider.go` / `approval_service.go` / `activity_sink.go` | The concrete implementations of the `httpapi` ports, one per role: `StatusReader` (read-adapter → status snapshot), `ApprovalService` (the voice-approval use case: validate id + send the BLE permission decision + update counters), `LogActivitySink` (logs the Claude Code pushes — the seam a future HAL bridge replaces). `main.go` is the composition root that wires these into `httpapi.New`. |
| `agent.go` | BlueZ `org.bluez.Agent1` (DisplayOnly) so LE pairing can complete; logs the passkey to the journal. |
| `transfer.go` | Folder-push receiver — streams files from Desktop to `/opt/claude-desktop-buddy/chars` with path-traversal guards. |
| `narrator.go` + `i18n.go` | Short, per-turn-deduped TTS announcements of Claude's activity, localized (EN/VI). |
| `stats.go` | Persists lifetime approved/denied counters to `/var/lib/claude-desktop-buddy/stats.json`. |
| `config/buddy.json` | Runtime config (see [`setup.md`](setup.md#configuration)). |
| `skill/SKILL.md` | The OpenClaw skill that turns approvals into a voice interaction. |
| `third_party/bluetooth/` | Local fork of `tinygo.org/x/bluetooth` (see below). |

## Data flow

### Inbound (Desktop → device)

1. **BLE write** lands on the RX characteristic. `ble.go:handleRX` appends bytes to
   a buffer and splits on `\n`; each complete line is pushed to `msgCh`.
2. A single **processor goroutine** drains `msgCh` and calls `handleBLEMessage`
   (in `main.go`) one line at a time, so the shared transfer state is race-free.
3. `ParseOrSalvage` (`protocol.go`) decodes the line into a typed message, or
   recovers the tail of a corrupted one (BLE write-without-response has no ACK, so
   BlueZ silently drops packets under load).
4. Dispatch by type:
   - **`Heartbeat`** → `StateMachine.HandleHeartbeat` → may transition state → fires
     `OnStateChange` → `bridge` repaints LED/display + `narrator` announces.
   - **`Event`** (`evt:"turn"`) → `bridge.OnEvent` publishes to Lamp's monitor bus;
     assistant turns are inspected block-by-block so `thinking`/`tool_use` blocks
     become TTS narration.
   - **`Command`** (`status`, `owner`, `name`, `unpair`, folder-push) → handled and
     **acked** over the TX characteristic.
   - **`TimeSync`** → logged (no ack required).

### Outbound (device → Desktop)

`ble.go:Send` writes newline-terminated JSON to the TX characteristic, chunked at
180 bytes (under the macOS-negotiated MTU). Two things go out this way:
- **Acks** for every received `Command` (`protocol.go:MakeAck` / `MakeAckN` /
  `MakeStatusAck`).
- **Permission decisions** (`MakePermission`) when the agent approves/denies.

### The approval round-trip

```
Desktop heartbeat carries `prompt`  ─►  state = attention, pending prompt stored
        │                                         │
        │                                bridge.postSensingEvent
        │                                  → Lamp /api/sensing/event
        │                                    type=buddy_approval  ─► agent hears it,
        │                                                            asks the user
        │                                                                  │
   GET /status (skill polls) ◄────────────────────────────────────────────┘
        │  pending_prompt {id, tool, hint}
        ▼
   user says yes/no  ─►  POST /claude-desktop/approve|/deny {id}
        │
        ▼
   ApprovalService validates id == pending.id
        │
        ▼
   ble.Send(MakePermission(id, "once"|"deny"))  ─►  Desktop applies the decision
        │
        ▼
   StateMachine.Approved()/Denied()  ─►  counters++ , stats persisted
```

`POST /claude-desktop/approve` sends decision `"once"`; `POST /claude-desktop/deny`
sends `"deny"`. Both return `409` if there is no pending prompt or the `id` doesn't
match the current one.

## State machine

States (`state.go`): `sleep`, `idle`, `busy`, `attention`, `heart`, `celebrate`.

| State | When | Lamp expression (`bridge.go`) |
|-------|------|-------------------------------|
| `sleep` | BLE disconnected | restore user's LED, sleepy eyes |
| `idle` | connected, nothing running | restore user's LED, eyes-mode |
| `busy` | heartbeat `running > 0` | pulse (Claude brand color), info: tokens today + sessions |
| `attention` | heartbeat carries a `prompt` | blink, info: "Approve `<tool>`?", emits `buddy_approval` sensing event |
| `heart` | approval granted within 5 s of the prompt | solid color, happy eyes (3 s, transient) |
| `celebrate` | token milestone crossed (every 50 000) | rainbow, excited eyes (3 s, transient) |

Transient states (`heart`, `celebrate`) hold for 3 s and are not overridden by
incoming heartbeats; a 500 ms ticker (`CheckTransientExpiry`) re-derives the real
state when they expire. Connect/disconnect is detected from the first heartbeat
and the BlueZ connect handler.

## The bridge (south side)

All Buddy LED writes are marked `transient: true`: they paint the strip without
overwriting the user's saved LED state. On return to `idle`/`sleep`, `ledRestore()`
asks LeLamp to repaint whatever the user had set, so Buddy never steals the strip
permanently. The accent color is the Claude app color **`#C15F3C`** (`claudeBrand`).

| Call | Endpoint | Purpose |
|------|----------|---------|
| `ledSolid` / `ledEffect` / `ledRestore` / `ledOff` | LeLamp `/led/*` | state LED cues (all transient) |
| `displayInfo` / `displayEyes` / `displayEyesMode` | LeLamp `/display/*` | round-display text + eyes |
| `expressEmotion` | LeLamp `/emotion` | coordinated LED+servo (e.g. "happy" when a turn ends) |
| `speakTTS` (cached) / `prerenderTTS` | LeLamp `/voice/speak` | narration TTS (and cache warmup at startup) |
| `postBuddyState` | Lamp `/api/monitor/event` (`type=buddy_state`) | surface state on the monitor |
| `postSensingEvent` | Lamp `/api/sensing/event` (`type=buddy_approval`) | inject approval into the sensing pipeline |
| `OnEvent` | Lamp `/api/monitor/event` (`type=buddy_event`) | surface chat turns for other use cases |

All bridge calls are fire-and-forget with a 5 s timeout; LeLamp's own `409`
(music busy) / `503` (TTS not ready) responses are ignored here.

## Narration (UC-9)

`narrator.go` emits short status phrases as TTS. Policy:
- **Per-turn dedupe** — each category fires at most once per turn (a turn starts on
  a new user message or an idle→busy transition), so multiple `thinking` blocks
  don't repeat the phrase.
- **Tool mapping** — `i18n.go:toolToCategory` maps Claude Code tool names to a
  phrase (`Read`→"reading a file", `Bash`→"running a shell command", `mcp__*`→
  "calling MCP", unknown→generic "using a tool" with the raw name dropped — tool
  names don't read well through TTS).
- **Warmup** — at startup every phrase is sent to LeLamp with `prerender:true` so
  the first real announcement plays from the on-disk TTS cache instead of waiting
  on a provider round-trip.
- **Language** — `narration_lang` in config (`en`/`vi`); unknown values fall back
  to English.

## Persistence

- **Stats** — lifetime approved/denied counters live at
  `/var/lib/claude-desktop-buddy/stats.json` (under `/var/lib` so they survive
  package upgrades that wipe `/opt`, and separate from the config so a config
  reset doesn't zero them). Restored on boot so `/status` is correct right after a
  restart.
- **Pushed folders** — `/opt/claude-desktop-buddy/chars/<name>/...`.

## The tinygo BLE fork

`go.mod` replaces `tinygo.org/x/bluetooth` with the local
`third_party/bluetooth`. Upstream's Linux GATT layer only maps the six basic
characteristic flags; the fork adds secure-read / secure-write so the Hardware
Buddy bonding requirement can be expressed. (Currently the secure-only flags are
*dropped* at runtime — see the note in `ble.go` — because the Mac client connects
without auto-triggering SMP; the link is unencrypted for now and `status.sec` is
reported `false`.)

## Notes / gotchas

- **`buddy.json` `led_mapping` is ignored.** The `Config` struct in `main.go` has
  no field for it; LED behavior is hardcoded in `bridge.go`. The block is legacy.
- **`approval_timeout_sec` is parsed but unused.** The 5 s "heart" window is
  hardcoded in `state.go`; there is no server-side approval timeout today.
- **Device name** — `device_name` may contain `{MAC}`, expanded at startup from
  Lamp's `/api/system/network` (last 4 hex of the Pi serial / eth0 MAC), matching
  the mDNS hostname `lamp-xxxx.local`. Truncated to 4 chars to fit the 31-byte BLE
  advertisement.
