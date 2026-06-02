# MQTT — Documentation

## Overview

Lamp uses MQTT to communicate with the backend server (status reporting, OTA commands, channel management).

- Client: Eclipse Paho autopaho (Go)
- Auto-reconnect on connection loss
- Client ID format: `lamp-device-{DeviceID}`

## Configuration

```json
// config/config.json
{
  "mqtt_endpoint": "broker.example.com",
  "mqtt_port": 8883,
  "mqtt_username": "...",
  "mqtt_password": "...",
  "fa_channel": "fa/{device_id}",
  "fd_channel": "fd/{device_id}"
}
```

## Topics

| Topic | Direction | Description |
|-------|-----------|-------------|
| `fa_channel` | Server → Device | Commands from backend (from-agent) |
| `fd_channel` | Device → Server | Responses from device (for-device) |

## Commands

### Envelope Format

```json
{
  "cmd": "info|add_channel|slack_event|whatsapp_pair|ota|data",
  ...payload fields
}
```

### `info` — Report device information

**Receive:** `{"cmd": "info"}`

**Response (publish fd_channel):**
```json
{
  "device": "ai-lamp",
  "type": "info",
  "version": "0.0.35",
  "id": "{DeviceID}",
  "mac": "{MAC address}",
  "time": "2026-03-26T17:00:00Z"
}
```

### `add_channel` — Add messaging channel

**Receive:**
```json
{
  "cmd": "add_channel",
  "channel": "telegram|slack|discord|whatsapp",
  "config": {
    // telegram: bot_token + chat_id
    // slack:    bot_token + app_token + channel_id        (socket mode, default)
    // slack:    bot_token + mode:"http" + signing_secret  (+ optional webhook_path, default /slack/events)
    // discord:  bot_token + guild_id  + user_id
    // whatsapp: user_id (E.164 phone — only field; the bot logs in via Baileys)
  }
}
```

**Slack transport modes.** `mode` selects how OpenClaw receives Slack events:

- **`socket`** (default when `mode` is omitted) — OpenClaw opens an outbound WebSocket to Slack; requires `app_token`. Existing installs are unaffected.
- **`http`** — OpenClaw listens for Slack Events API POSTs at `webhook_path` (default `/slack/events`) and re-verifies the Slack signature with `signing_secret`; `app_token` is not used. A public proxy (bff-campaign-service) receives Slack's HTTP events and fans them out to the owning device over MQTT as `slack_event` (below). HTTP mode is the message-loss-tolerant path because Slack retries failed deliveries ~3× over 5 min.

**Response (single — telegram/slack/discord):**
```json
{
  "device": "ai_lamp",
  "type": "add_channel",
  "channel": "telegram",
  "status": "success|failure",
  "error": "..."
}
```

**Response (streamed — whatsapp):** the device publishes one fd_channel message
per pairing event:

1. `{"status":"pairing_starting"}` — CLI subprocess launched.
2. `{"status":"pairing_qr","pairing_qr_text":"<unicode-block grid>","pairing_qr_format":"unicode_blocks_2x1","pairing_qr_seq":1,"pairing_expires_at":"<RFC3339>"}` — repeated up to 5 times as Baileys rotates the QR (~20s each).
3. One terminal event:
   - `{"status":"success"}` — link confirmed; emitted after a 5-minute post-pair sync wait so Baileys' history/pre-keys finish loading before the operator is told the channel is ready.
   - `{"status":"timeout","error":"..."}` — operator did not scan within the QR window.
   - `{"status":"failure","error":"..."}` — CLI exited unexpectedly or another pairing was already in progress.

If a Baileys session already exists on disk (`<openclaw_config_dir>/credentials/whatsapp/default/creds.json`), the device skips QR rendering and publishes just `{"status":"success"}`.

### `whatsapp_pair` — Re-run WhatsApp pairing

Re-runs the QR-scan flow without re-bootstrapping the channel config. Used when the Baileys session was lost and needs re-linking.

**Receive:** `{"cmd": "whatsapp_pair"}`

**Response (streamed):** same shape as the whatsapp `add_channel` stream above, but `type:"whatsapp_pair"`. Timeout 120 s (vs. 10 min for `add_channel`) — no plugin install or restart on this path.

### `slack_event` — Forward a Slack Events API delivery (HTTP mode)

Sent by the public Slack-events proxy (bff-campaign-service) when Slack delivers an
Events API POST for a workspace this device owns. The payload is a verbatim forward of
Slack's HTTP request body + signature headers; the device POSTs them to the local
OpenClaw gateway's `webhook_path` (default `http://127.0.0.1:18789/slack/events`), which
re-verifies the Slack signature against the shared `signing_secret`. Only relevant when
the device's slack channel is configured with `mode:"http"` (see `add_channel`).

**Receive:**
```json
{
  "cmd": "slack_event",
  "event_id": "Ev123",
  "body": "<raw Slack JSON body>",
  "headers": {
    "X-Slack-Signature": "v0=...",
    "X-Slack-Request-Timestamp": "...",
    "Content-Type": "application/json"
  }
}
```

The device dedups on `event_id` with a 5-minute in-memory LRU (matches Slack's retry
window) and forwards headers verbatim so OpenClaw's signature check validates.

**Response (publish fd_channel):**
```json
{
  "channel": "slack",
  "type": "slack_event",
  "event_id": "Ev123",
  "status": "success|failure|skipped_duplicate",
  "error": "...",
  "http_status": 200,
  "info": { /* same device/version metadata as other acks */ }
}
```

For the proxy to route inbound events back to the right device, each `/ping` includes
`slack_team_id` — the workspace ID the device resolves on-device via Slack `auth.test`
against its stored `botToken` (cached, sent once resolved).

### `data` — Generic data envelope

A generic envelope whose `kind` selects a sub-handler. The optional `data` object
carries kind-specific fields. Every kind replies on fd_channel with the same shape:
the standard device/version metadata plus `kind`, `status` (`success|failure`),
optional `error`, and an optional `data` payload.

**Receive:** `{"cmd": "data", "kind": "<kind>", "data": { ... }}`

| Kind | Purpose | `data` fields |
|------|---------|---------------|
| `tts.set` | Persist TTS voice/provider/language config | `provider`, `voice`, `language` |
| `tts.preview` | One-shot TTS preview (no config write) | `text` (required), optional `provider`/`voice`/`language` |
| `oauth.set` | Store/replace an OAuth token for a provider | `provider`, `access_token`, optional `refresh_token`/`token_type`/`expires_at`/`scopes`/`user_email`/`client_id` |
| `oauth.remove` | Delete the stored OAuth token for a provider | `provider` |
| `system.info` | Aggregate snapshot: versions + network + host | _(none)_ |
| `system.version` | Component versions only (cheaper than `system.info`) | _(none)_ |
| `system.network` | wlan0 network facts only | _(none)_ |

**`system.info` response:** synchronous (no `starting` intermediate); each probe
falls back to its zero value on failure.
```json
{
  "device": "ai_lamp",
  "type": "data",
  "kind": "system.info",
  "status": "success",
  "data": {
    "versions": {
      "lamp": "0.0.35",
      "bootstrap": "0.0.10",
      "lelamp": "1.2.3",
      "openclaw": "2026.5.27",
      "openclaw_detected": true
    },
    "network": {
      "private_ip": "192.168.1.42",
      "interface": "wlan0",
      "mac": "aa:bb:cc:dd:ee:ff",
      "ssid": "MyWiFi",
      "gateway": "192.168.1.1"
    },
    "host": {
      "hostname": "ai-lamp",
      "device_id": "{DeviceID}",
      "device_name": "Lamp-7f72",
      "uptime_seconds": 86400
    }
  }
}
```

`system.version` returns just the `versions` block as `data`; `system.network`
returns just the `network` block. Version probes: `lamp` from the ldflags build
var, `bootstrap` via `bootstrap-server --version`, `lelamp` over HTTP from the
local LeLamp `/version` endpoint, `openclaw` from the agent monitor's cached probe
(`openclaw_detected` distinguishes "not installed" from "installed but unparseable").

An unrecognized `kind` replies with `status:"failure"` and `error:"unknown kind: <kind>"`.

### `ota` — Trigger OTA update

Handled by bootstrap worker, not through MQTT handler directly.

## Code

| File | Role |
|------|------|
| `lamp/lib/mqtt/client.go` | MQTT client (connect, subscribe, publish) |
| `lamp/lib/mqtt/config.go` | Config struct |
| `lamp/lib/mqtt/options.go` | Connection options |
| `lamp/lib/mqtt/factory.go` | Factory to create client with unique ID |
| `lamp/server/device/delivery/mqtt/handler.go` | Command dispatcher |
| `lamp/server/device/delivery/mqtt/info_handler.go` | Handle `info` command |
| `lamp/server/device/delivery/mqtt/add_channel_hander.go` | Handle `add_channel` command (streams pairing events for WhatsApp) |
| `lamp/server/device/delivery/mqtt/slack_event_handler.go` | Handle `slack_event` command (forwards Slack HTTP-mode events to local gateway) |
| `lamp/server/device/delivery/mqtt/data_handler.go` | Handle `data` command kinds `oauth.set`/`oauth.remove` (+ access-token store) |
| `lamp/server/device/delivery/mqtt/system_info_handler.go` | Handle `data` kinds `system.info`/`system.version`/`system.network` |
| `lamp/server/device/delivery/mqtt/whatsapp_pair_handler.go` | Handle `whatsapp_pair` re-pair command |
| `lamp/internal/openclaw/pairing.go` | WhatsApp Baileys QR pairing subprocess driver |
| `lamp/domain/device.go` | MQTTMessage, command constants |
| `lamp/domain/pairing.go` | PairingEvent + status enum |
