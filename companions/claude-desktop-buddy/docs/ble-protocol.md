# BLE Protocol

The device and Claude Desktop talk over a Bluetooth LE **Nordic UART Service
(NUS)**. The wire format is **newline-delimited JSON** — each message is a single
JSON object followed by `\n`. This document is the reference for that format; the
implementation lives in `ble.go` (transport) and `protocol.go` (messages).

## GATT layout

| Item | UUID | Direction | Flags |
|------|------|-----------|-------|
| Service (NUS) | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` | — | — |
| RX characteristic | `6e400002-b5a3-f393-e0a9-e50e24dcca9e` | Desktop → device | Write, Write-Without-Response |
| TX characteristic | `6e400003-b5a3-f393-e0a9-e50e24dcca9e` | device → Desktop | Notify, Read |

The device is the **peripheral**: it advertises the NUS service UUID and its local
name (e.g. `Claude-lamp-a1b2`). Claude Desktop is the **central** that scans,
connects, and subscribes to TX notifications.

### Advertising timing

tinygo's Linux backend leaves advertising at BlueZ's 1.28 s default, which is too
slow for macOS scan windows. On start, `tuneAdvIntervals()` writes
`adv_min_interval=160` (100 ms) and `adv_max_interval=320` (200 ms) — units of
0.625 ms — to every `/sys/kernel/debug/bluetooth/hci*` debugfs knob. Best-effort:
if debugfs isn't available the device falls back to the BlueZ default.

## Framing

- **Inbound:** bytes from the RX characteristic are buffered and split on `\n`
  (`handleRX`). A partial line stays buffered until its terminating newline arrives
  in a later write. The buffer is reset on disconnect.
- **Outbound:** `Send` appends `\n` and writes the line to TX in **180-byte
  chunks** (under the ~185 B macOS-negotiated MTU) so a typical ack fits in one
  notification.

### Packet-loss salvage

Claude Desktop writes RX via **Write-Without-Response**, which has no ATT
confirmation, so BlueZ can silently drop packets under load. `ParseOrSalvage`
handles the fallout:

1. Try to parse the whole line. If it succeeds, done (0 bytes lost).
2. Otherwise scan for the **last** occurrence of a known opener — `{"cmd":"`,
   `{"time":`, `{"total":`, `{"evt":"` — that parses, and use the tail from there.
3. If nothing parses, the line is dropped and categorized in the log as
   `prefix-lost` (head gone), `truncated` (tail gone, no closing `}`), or
   `mid-corruption` (a chunk inside an array dropped).

Any framing loss **aborts an in-progress folder transfer**, since its byte stream
can no longer be trusted.

## Inbound messages (Desktop → device)

The parser dispatches on the presence of a discriminator field, in this order:
`cmd` → `time` → `evt` → `total`.

### Heartbeat

Sent every ~10 s and on state change. Drives the state machine. Distinguished by
having a `total` field (and no `cmd`/`time`/`evt`).

```json
{
  "total": 3, "running": 1, "waiting": 0,
  "msg": "Editing handler.go", "entries": ["...", "..."],
  "tokens": 152340, "tokens_today": 482000,
  "prompt": { "id": "p-9f3", "tool": "Bash", "hint": "rm -rf build/" }
}
```

| Field | Meaning |
|-------|---------|
| `total` / `running` / `waiting` | session counts; `running > 0` → state `busy` |
| `msg` | current status text (shown on the monitor) |
| `entries` | recent activity lines |
| `tokens` / `tokens_today` | usage; crossing a 50 000 multiple of `tokens` → `celebrate` |
| `prompt` | **present only when permission is required** → state `attention` |

`prompt` = `{ id, tool, hint }`. The device logs heartbeats only when something
meaningful changed (running / waiting / msg / prompt arrival or clear); token
counts alone don't trigger a log line.

### Event

Streams chat turns and other Desktop events. Distinguished by `evt`.

```json
{ "evt": "turn", "role": "assistant", "content": [ {"type":"tool_use","name":"Read","input":{...}} ] }
{ "evt": "turn", "role": "user", "content": "fix the build" }
```

- `content` is **either** a bare string (user turns) **or** an array of Anthropic
  content blocks (assistant turns + tool results).
- Recognized block types: `text`, `thinking`, `tool_use` (`name`,`input`),
  `tool_result` (`tool_use_id`,`content`), `tool_reference` (`tool_name`).
- The device fans events to Lamp's monitor bus and narrates `thinking` / `tool_use`
  blocks as TTS. No ack required.

### Command

Control + folder-push. Distinguished by `cmd`.

```json
{ "cmd": "status" }
{ "cmd": "owner", "name": "Leo" }
{ "cmd": "name",  "name": "Lamp" }
{ "cmd": "unpair" }
```

Folder-push sub-protocol (stream a folder to the device):

| `cmd` | Fields | Action |
|-------|--------|--------|
| `char_begin` | `name`, `total` (bytes) | start a transfer into `chars/<name>/` |
| `file` | `path` (relative), `size` | open a new file |
| `chunk` | `d` (base64) | append decoded bytes to the current file |
| `file_end` | — | close the current file |
| `char_end` | — | finish the transfer |

Paths are sanitized: no absolute paths, no `..` traversal, no separators in
`name`. Every command is acked (see below). Destination root is
`/opt/claude-desktop-buddy/chars`.

### TimeSync

```json
{ "time": [1717843200, -25200] }
```

`[epoch_seconds, utc_offset_seconds]`, sent on connect. Logged; no ack.

## Outbound messages (device → Desktop)

### Ack

Sent for every received `Command`.

```json
{ "ack": "chunk", "ok": true, "n": 4096 }
```

`n` is a byte count, used for `chunk` (bytes in current file) and `file_end`.
`MakeAck` sets `n:0`; `MakeAckN` carries the count.

### Status ack

The reply to `{ "cmd": "status" }` carries device info (keys are abbreviated to
match Claude Desktop's expectations):

```json
{
  "ack": "status", "ok": true,
  "data": {
    "name": "Claude-lamp-a1b2",
    "sec": false,
    "bat": { "pct": 100, "mV": 5000, "mA": 0, "usb": true },
    "sys": { "up": 3600, "heap": 0 },
    "stats": { "appr": 12, "deny": 3, "vel": 0, "nap": 0, "lvl": 0 }
  }
}
```

`sec` (link encrypted) is `false` today — see the bonding note in
[`architecture.md`](architecture.md#the-tinygo-ble-fork). `bat` is fixed (the Pi
is always on USB). `appr`/`deny` are the lifetime approval/denial counters.

### Permission decision

Sent when the agent approves or denies a prompt (via the HTTP API):

```json
{ "cmd": "permission", "id": "p-9f3", "decision": "once" }
```

`decision` is `"once"` (approve) or `"deny"`. The `id` must match the prompt's
`id` from the heartbeat.

## Message summary

| Direction | Type | Discriminator | Ack? |
|-----------|------|---------------|------|
| Desktop → device | Heartbeat | `total` | no |
| Desktop → device | Event | `evt` | no |
| Desktop → device | Command | `cmd` | **yes** |
| Desktop → device | TimeSync | `time` | no |
| device → Desktop | Ack / Status ack | `ack` | — |
| device → Desktop | Permission | `cmd:"permission"` | — |
