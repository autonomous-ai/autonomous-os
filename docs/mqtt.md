# MQTT — Documentation

## Overview

The OS server uses MQTT to communicate with the backend server (status reporting, OTA commands, channel management).

- Client: Eclipse Paho autopaho (Go)
- Auto-reconnect on connection loss
- Client ID format: `device-{DeviceID}`

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
  "cmd": "info|add_channel|slack_event|slack_command|whatsapp_pair|ota|data",
  ...payload fields
}
```

### `info` — Report device information

**Receive:** `{"cmd": "info"}`

**Response (publish fd_channel):**
```json
{
  "device": "lamp",
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
  "device": "lamp",
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

### `slack_command` — Forward a Slack slash command (HTTP mode)

Sent by the same Slack proxy (bff-campaign-service) when Slack delivers a slash-command
invocation (`/openclaw`, `/new`, ...) for a workspace this device owns. Forwarded and
verified exactly like `slack_event`: the device POSTs the verbatim body + signature
headers to the **same** OpenClaw gateway `webhook_path` (default
`http://127.0.0.1:18789/slack/events`) — OpenClaw's single HTTP endpoint routes events
vs. commands by body shape (urlencoded `command=` vs. JSON `type`) and replies to the
user via the command's `response_url`. Only relevant when the device's slack channel is
configured with `mode:"http"` (see `add_channel`).

**Receive:**
```json
{
  "cmd": "slack_command",
  "event_id": "<trigger_id>",
  "body": "<raw urlencoded form body>",
  "headers": {
    "X-Slack-Signature": "v0=...",
    "X-Slack-Request-Timestamp": "...",
    "Content-Type": "application/x-www-form-urlencoded"
  }
}
```

Differences from `slack_event`: the body is the urlencoded slash-command form (it carries
`command`, `text`, `response_url`, `trigger_id`, ...), the `Content-Type` is
`application/x-www-form-urlencoded`, and the `event_id` slot carries Slack's `trigger_id`
(slash commands have no `event_id`) — reused as the dedup key.

**Response (publish fd_channel):** same shape as `slack_event` but `type:"slack_command"`.

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
| `connector.set.<code>` | Store/replace credentials for a connector (async; acks `starting`) | `connector`, `auth_type`, optional `access_token`/`refresh_token`/`api_key`/`expires_in`/`expires_at`/`scopes`/`credentials`/`refresh` |
| `connector.remove.<code>` | Delete a connector's credentials (async; acks `starting`) | `connector` |
| `system.info` | Aggregate snapshot: versions + network + host | _(none)_ |
| `system.version` | Component versions only (cheaper than `system.info`) | _(none)_ |
| `system.network` | wlan0 network facts only | _(none)_ |

**`system.info` response:** synchronous (no `starting` intermediate); each probe
falls back to its zero value on failure.
```json
{
  "device": "lamp",
  "type": "data",
  "kind": "system.info",
  "status": "success",
  "data": {
    "versions": {
      "os-server": "0.0.35",
      "bootstrap": "0.0.10",
      "hal": "1.2.3",
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
      "hostname": "lamp-7f72",
      "device_id": "{DeviceID}",
      "device_name": "lamp-7f72",
      "uptime_seconds": 86400
    }
  }
}
```

`system.version` returns just the `versions` block as `data`; `system.network`
returns just the `network` block. Version probes: `os-server` from the ldflags build
var, `bootstrap` via `bootstrap-server --version`, `hal` over HTTP from the
local HAL `/version` endpoint, `openclaw` from the agent monitor's cached probe
(`openclaw_detected` distinguishes "not installed" from "installed but unparseable").

An unrecognized `kind` replies with `status:"failure"` and `error:"unknown kind: <kind>"`.

#### Connectors

`connector.set.<code>` / `connector.remove.<code>` route by prefix (the connector
code is the suffix). A single **data-driven** writer (`connectorWriter`) handles
every connector; a small map of **special writers** claims the handful of codes that
can't be expressed as a plain HTTP MCP entry (today only `figma-api`, a local stdio
MCP server). The generic writer decides per-message — from the payload — whether the
connector is an MCP server and how to authenticate; there is **no per-connector
registry to update for a new connector**.

**Storage:** every connector persists to its own `<code>_access_tokens.json` under
`workspace/configs/` (atomic tmp+rename, mode 0600). The connector code is validated
against `^[a-z0-9_-]{1,64}$` before it is used as a filename or `mcp.servers.<code>`
key, so an untrusted code cannot escape the configs dir via path traversal.

**Routing (per `connector.set` payload):** the backend sets routing keys in the
payload's `credentials` map:

| `credentials` key | Effect |
|-------------------|--------|
| `mcp_url` | Present → MCP connector: writes `mcp.servers.<code>` (`{type:"http", url, headers.Authorization}`) into `openclaw.json` and restarts the gateway. Absent → credential-only connector (e.g. `gmail`/`google_*`): token stored, **no** `openclaw.json` entry. |
| `mcp_auth_header` | `bearer_access_token` (default) → `Authorization: Bearer <access_token>`; `bearer_api_key` → `Bearer <api_key>` (static-key connectors, e.g. `ahrefs`); `header:<Name>` → raw header `<Name>: <token>` with no Bearer prefix (token prefers `api_key`, falls back to `access_token`) for non-Bearer providers, e.g. a Figma PAT via `header:X-Figma-Token`. A PAT connector relays `auth_type:"pat"` with the token in `api_key`. |

**Fallback table:** for connectors that shipped before the wire carried these keys
(`notion`, `asana`, `linear`, `github`, `ahrefs`), a compiled-in table
supplies the `mcp_url` + header style from the openclaw catalog
(`internal/openclaw/mcp.go`). The payload **always wins** — `mcp_url` in the payload
overrides the fallback — so the table is only a migration safety net until the
backend pushes the routing keys.

**Special writers:** `figma-api` uses the hosted Figma MCP allowlist workaround — a
local stdio MCP server (`{command:"node", args:[wrapper], env:{FIGMA_ACCESS_TOKEN}}`)
whose Node wrapper is dropped on disk before the entry is written. Special-writer
codes are excluded (`reserved`) from the generic writer's refresh scan so it never
re-writes them in the wrong (HTTP) shape.

**Refresh:** the refresh loop scans the generic writer (globbing
`*_access_tokens.json`) plus each special writer, and proactively rotates any entry
carrying BOTH a `refresh_token` AND `refresh:true` (the backend owns refresh
eligibility via the `refresh` flag) once it is within 10 minutes of expiry, via the
backend `/connector/refresh-token` endpoint.

### `ota` — Trigger OTA update

Handled by bootstrap worker, not through MQTT handler directly.

## Code

| File | Role |
|------|------|
| `os/services/lib/mqtt/client.go` | MQTT client (connect, subscribe, publish) |
| `os/services/lib/mqtt/config.go` | Config struct |
| `os/services/lib/mqtt/options.go` | Connection options |
| `os/services/lib/mqtt/factory.go` | Factory to create client with unique ID |
| `os/services/server/device/delivery/mqtt/handler.go` | Command dispatcher |
| `os/services/server/device/delivery/mqtt/info_handler.go` | Handle `info` command |
| `os/services/server/device/delivery/mqtt/add_channel_hander.go` | Handle `add_channel` command (streams pairing events for WhatsApp) |
| `os/services/server/device/delivery/mqtt/slack_event_handler.go` | Handle `slack_event` / `slack_command` (forwards Slack HTTP-mode events and slash commands to local gateway) |
| `os/services/server/device/delivery/mqtt/data_handler.go` | Handle `data` command kinds `oauth.set`/`oauth.remove` (+ access-token store) |
| `os/services/server/device/delivery/mqtt/connector_handler.go` | Handle `connector.set.<code>`/`connector.remove.<code>` (async, writer dispatch via `connectorWriterFor`) |
| `os/services/server/device/delivery/mqtt/connector_writer.go` | `ConnectorWriter` interface + shared `<code>_access_tokens.json` file helpers |
| `os/services/server/device/delivery/mqtt/connector_writer_generic.go` | Data-driven `connectorWriter`: payload-driven MCP routing, fallback table, path-traversal guard, per-connector token files |
| `os/services/server/device/delivery/mqtt/mcp_connector_writer.go` | Special stdio MCP writer (`figma-api`): token file + local-wrapper `openclaw.json` MCP entry |
| `os/services/server/device/delivery/mqtt/connector_refresh.go` | Connector token refresh loop (`/connector/refresh-token`) |
| `os/services/server/device/delivery/mqtt/system_info_handler.go` | Handle `data` kinds `system.info`/`system.version`/`system.network` |
| `os/services/server/device/delivery/mqtt/whatsapp_pair_handler.go` | Handle `whatsapp_pair` re-pair command |
| `os/services/internal/openclaw/pairing.go` | WhatsApp Baileys QR pairing subprocess driver |
| `os/services/domain/device.go` | MQTTMessage, command constants |
| `os/services/domain/pairing.go` | PairingEvent + status enum |
