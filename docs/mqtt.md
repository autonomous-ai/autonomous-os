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
  "cmd": "info|add_channel|slack_event|slack_command|discord_event|discord_reply|discord_typing|whatsapp_pair|claudecode_login|claudecode_login_code|ota|data",
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
  "time": "2026-03-26T17:00:00Z",
  "agent_runtime": "openclaw"
}
```

`agent_runtime` is the **effective** agentic backend currently running
(`openclaw` | `hermes` | `picoclaw` | `codex` | `claudecode` | `opencode`) — resolved as
`config.agent_runtime`, else the device's `DEVICE.md` `gateway.default`, else
`openclaw`. The response also carries these optional fields when known:
`hal_version`, `openclaw_version`, `hermes_version`, `picoclaw_version`,
`codex_version`, `claudecode_version`, `opencode_version`, `local_ip`, `tts_provider`, `tts_voice`,
`stt_language`, `timezone`, `unsupported_channels`. `timezone` is the device's
**live** IANA zone (e.g. `Asia/Ho_Chi_Minh`), read fresh from `/etc/timezone`
(falling back to config), not just the config record. The six per-runtime
versions are all probed at startup (each from its own `--version`) and
reported side by side; `agent_runtime` names the active one.

`unsupported_channels` (omitted when empty) lists the channels configured on the
device that the **active** runtime cannot run. It is populated by `ChannelReconcile`
after a runtime switch — e.g. switching `openclaw` → `picoclaw` (telegram-only) leaves
any configured `slack`/`discord` as unsupported. The list is sourced from
`config.channels_unsupported`, which `ChannelReconcile` rewrites on each switch.

**HTTP backend ping mirrors these fields.** The device-initiated ping
(`POST {llm_base}/ping`, built by `system/device.buildPingPayload`, sent via
`system/beclient`) carries the same device-state fields as this `info` uplink —
`local_ip`, `device`, `device_id`, `timezone`, `tts_provider`, `tts_voice`,
`stt_language`, `hal_version`, `unsupported_channels` — plus `agent_runtime` and
`agent_runtime_version`. Unlike `info` (which reports every installed backend's
version side by side), the ping sends **only the active runtime's version**. It
fires (1) right after WiFi join during setup (status `setting_up`,
fire-and-forget — publishes `local_ip` before the up-to-2-min agent setup, so
the Setup-popup rescue described in `docs/setup-flow.md` can work), (2) once
when setup completes (status `working`), and (3) periodically from the status
reporter. Fields the backend doesn't consume are simply ignored.

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
    // discord:  bot_token + guild_id  + user_id            (bring-your-own-bot)
    // discord:  managed:true + guild_id + user_id          (managed shared-bot — no bot_token on device)
    // whatsapp: user_id (E.164 phone — only field; the bot logs in via Baileys)
  }
}
```

**Slack transport modes.** `mode` selects how OpenClaw receives Slack events:

- **`socket`** (default when `mode` is omitted) — OpenClaw opens an outbound WebSocket to Slack; requires `app_token`. Existing installs are unaffected.
- **`http`** — OpenClaw listens for Slack Events API POSTs at `webhook_path` (default `/slack/events`) and re-verifies the Slack signature with `signing_secret`; `app_token` is not used. A public proxy (bff-campaign-service) receives Slack's HTTP events and fans them out to the owning device over MQTT as `slack_event` (below). HTTP mode is the message-loss-tolerant path because Slack retries failed deliveries ~3× over 5 min.

**Managed Discord (shared-bot).** When the discord `config` carries `managed:true`,
the device stores **no** `discord_bot_token`: the shared Autonomous bot's token lives
only in the cloud Discord relay (bff-campaign-service, sibling of the Slack proxy). The
device holds just its per-device `llm_api_key` (lobster key), which the relay reuses to
route. Inbound messages arrive over MQTT as `discord_event` and replies go back as
`discord_reply` / `discord_typing` (below), mirroring the Slack HTTP fan-out. The device
layer capability-gates managed Discord on the active runtime implementing
`domain.DiscordBridge` and clears any on-device token when `managed` is set
(`system/device/channels.go` `AddChannel`). Legacy bring-your-own-bot Discord
(device-held `bot_token` + a local Gateway session) is unchanged when `managed` is
absent/false.

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

**Capability gate.** `add_channel` is capability-aware: when the **active** agent
runtime cannot run the requested channel, the device replies `status:"failure"` with
the stable error code `error:"channel_not_supported"` (mapped from
`domain.ErrChannelNotSupported` via `errors.Is`, mirroring how `channel.refresh_config`
maps its sentinels). Previously every runtime silently accepted any channel. Each
runtime declares its own `SupportedChannels` — e.g. `picoclaw` runs telegram only, so
`slack`/`discord`/`whatsapp` return `channel_not_supported`.

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

### `claudecode_login` / `claudecode_login_code` — claude.ai OAuth login (claudecode runtime)

Runs the claude.ai subscription login (`claude setup-token`) on a device whose active
runtime is claudecode, so the brain authenticates with the user's Claude account
instead of `llm_api_key`. Only the claudecode runtime supports it — other runtimes
answer a one-shot `{"status":"failure","error":"claude login not supported on … backend"}`.
See `docs/agentic/claudecode.md` §"Auth".

**Receive:** `{"cmd": "claudecode_login"}`

**Response (streamed, `type:"claudecode_login"`):**

1. `{"status":"pairing_starting"}`
2. `{"status":"pairing_url","login_url":"https://claude.ai/oauth/authorize?..."}` — the
   user opens this URL in a browser, authorizes, and copies the code it shows.
3. terminal: `{"status":"success"}` (token persisted to config.json
   `claude_code_oauth_token`; presync flips the runtime to subscription auth and the
   bridge restarts) · `{"status":"timeout","error":"no login within 10m0s"}` ·
   `{"status":"failure","error":"..."}`.

**Receive (second leg):** `{"cmd": "claudecode_login_code", "code": "<pasted code>"}` —
feeds the browser code back into the waiting flow. Acked with
`{"status":"code_accepted"}` (or `{"status":"failure","error":"no claude login in progress"}`);
the flow's own terminal status still arrives on the `claudecode_login` stream.

Unlike `whatsapp_pair`, the login handler does not block MQTT dispatch while the flow
runs — the code arrives as a second MQTT command, which could never be dispatched if
the first handler held the loop.

### `slack_event` — Forward a Slack Events API delivery (HTTP mode)

Sent by the public Slack-events proxy (bff-campaign-service) when Slack delivers an
Events API POST for a workspace this device owns. The payload (a verbatim forward of
Slack's HTTP request body + signature headers) and the MQTT wire shape are unchanged —
but **how the device handles it now branches on the active runtime** (the handler
type-asserts the agent gateway to `domain.SlackBridge`):

- **Runtimes that serve the Slack webhook themselves** (not a `SlackBridge` — today:
  OpenClaw) — unchanged behavior: the device POSTs the verbatim body + signature headers to
  the local gateway's `webhook_path` (default `http://127.0.0.1:18789/slack/events`), which
  re-verifies the Slack signature against the shared `signing_secret`. The fd_channel ack
  carries the gateway's HTTP status. Only relevant when the device's slack channel is
  configured with `mode:"http"` (see `add_channel`).
- **Runtimes whose native Slack support is Socket Mode only** (implement
  `domain.SlackBridge`) — this branch is **generic to any such runtime** (hermes is the
  current example, not a special case): with only Socket Mode it has **no local HTTP Slack
  webhook**, so os-server **is** the HTTP-mode Slack frontend for it. It parses the event
  itself and drives a turn (`HandleInboundSlack`). The reply is rendered **straight to
  Slack via the Bot API**, not relayed back over MQTT, using Slack's **native streaming
  API**: `chat.startStream` (opens the streaming message) → `chat.appendStream`
  (progressive `markdown_text`) → `chat.stopStream` (finalize), plus
  `assistant.threads.setStatus` for the native "…is typing" indicator. The fd_channel ack
  still becomes `status:"success"` (`http_status` 200) once the inbound turn is dispatched.
  A `url_verification` challenge
  normally terminates at the public proxy (which owns the Slack Request URL), so it is
  handled defensively here and still acked `success`.

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

**Runtime support:** slash commands remain **OpenClaw-only**. The hermes `SlackBridge`
defers slash commands for now (v1) — only `slack_event` is runtime-aware — so on a hermes
device `slack_command` still follows the OpenClaw local-webhook path described above.

**Response (publish fd_channel):** same shape as `slack_event` but `type:"slack_command"`.

### `discord_event` — Forward a relayed Discord message (managed shared-bot)

Sent by the bff-campaign-service **Discord relay** when the shared Autonomous bot
receives a DM or guild message a device owns. Unlike `slack_event` (a verbatim HTTP
forward to a local webhook), in managed mode the device holds **no** Discord token and
opens **no** Gateway session, so the relay supplies the **fully-resolved** fields —
including `bot_user_id` and `mentions_bot`, which a device with a live session would
otherwise derive itself. The handler
(`system/server/device/delivery/mqtt/discord_event_handler.go`, `handleDiscordEvent`)
dedups on the Discord message id (reusing the shared `event_id` LRU that backs
`slack_event`), type-asserts the active gateway to `domain.DiscordBridge`, and injects
the message as a chat turn via `HandleInboundDiscord`. The reply is delivered
asynchronously via the `discord_reply` / `discord_typing` uplink (below). A runtime that
doesn't implement `DiscordBridge` acks `failure` with `error:"channel_not_supported"`, so
the relay stops routing.

**Receive:**
```json
{
  "cmd": "discord_event",
  "event_id": "<discord message id>",
  "discord_user_id": "<author id — the allowlist principal>",
  "guild_id": "<guild id; empty for a DM>",
  "discord_channel_id": "<channel to reply to>",
  "author_username": "<display name>",
  "bot_user_id": "<relay-supplied>",
  "mentions_bot": true,
  "text": "<raw content>"
}
```

**Response (publish fd_channel):** mirrors the `slack_event` ack vocabulary
(`publishDiscordResult` ↔ `publishSlackResult`):
```json
{
  "channel": "discord",
  "type": "discord_event",
  "event_id": "<discord message id>",
  "status": "success|failure|skipped_duplicate",
  "error": "...",
  "info": { /* same device/version metadata as other acks */ }
}
```

### `discord_reply` / `discord_typing` — Managed-Discord uplink (device → relay)

Published by the device on **fd_channel** — the outbound side of managed Discord. A
managed-Discord runtime routes its reply/typing to os-server's `ChannelRelay`
(`system/server/device/delivery/mqtt/channel_relay.go`, `mqttChannelRelay`), which
publishes these envelopes; the bff-campaign-service Discord relay consumes them and calls
Discord REST **as the shared bot**. `discord_reply` carries the **full** reply text — the
relay chunks it to Discord's **2000-character** limit; `discord_typing` triggers one
native typing indicator.

```json
{"cmd": "discord_reply",  "discord_channel_id": "...", "text": "..."}
{"cmd": "discord_typing", "discord_channel_id": "..."}
```

The relay is installed on the active gateway at startup via
`DiscordBridge.SetChannelRelay` (only when the runtime implements `DiscordBridge`), in
`ProvideDeviceMQTTHandler`.

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
| `timezone.set` | Apply the device's IANA timezone (async; acks `starting`) | `timezone` (required, e.g. `Asia/Ho_Chi_Minh`) |
| `oauth.set` | Store/replace an OAuth token for a provider | `provider`, `access_token`, optional `refresh_token`/`token_type`/`expires_at`/`scopes`/`user_email`/`client_id` |
| `oauth.remove` | Delete the stored OAuth token for a provider | `provider` |
| `connector.set.<code>` | Store/replace credentials for a connector (async; acks `starting`) | `connector`, `auth_type`, optional `access_token`/`refresh_token`/`api_key`/`expires_in`/`expires_at`/`scopes`/`credentials`/`refresh` |
| `connector.remove.<code>` | Delete a connector's credentials (async; acks `starting`) | `connector` |
| `channel.refresh_config` | Re-apply a channel's canonical config block (async; acks `configuring`) | `channel` |
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
      "openclaw": "2026.6.10",
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
      "uptime_seconds": 86400,
      "timezone": "Asia/Ho_Chi_Minh"
    }
  }
}
```

The `host.timezone` field is the device's **live** IANA zone, read fresh from the
system (`/etc/timezone`, falling back to config); omitted when it can't be resolved.

`system.version` returns just the `versions` block as `data`; `system.network`
returns just the `network` block. Version probes: `os-server` from the ldflags build
var, `bootstrap` via `bootstrap-server --version`, `hal` over HTTP from the
local HAL `/version` endpoint, `openclaw` from the agent monitor's cached probe
(`openclaw_detected` distinguishes "not installed" from "installed but unparseable").

An unrecognized `kind` replies with `status:"failure"` and `error:"unknown kind: <kind>"`.

#### `timezone.set`

Sets the device's IANA timezone. Same async shape as `realtime.set` / `tts.set`:
the device acks immediately, applies the change in the background, then acks the
outcome.

**Receive:** `{"cmd": "data", "kind": "timezone.set", "data": {"timezone": "Asia/Ho_Chi_Minh"}}`

**Ack flow** (each on fd_channel, carrying the standard device/version metadata plus
`kind:"timezone.set"`):

1. `{"status":"starting"}` — command received, before applying.
2. One terminal ack:
   - `{"status":"success","data":{"timezone":"Asia/Ho_Chi_Minh"}}` — applied (the
     requested zone is echoed back in `data`).
   - `{"status":"failure","error":"..."}` — rejected (e.g. unknown zone, or invalid
     JSON payload).

**Apply:** the zone is validated against `/usr/share/zoneinfo` (an unknown zone →
`failure`), then the device rewrites the `/etc/localtime` symlink, writes
`/etc/timezone`, runs `timedatectl set-timezone` best-effort, and persists `timezone`
to `config.json`. The change takes effect **without a HAL restart** — HAL's clock
helpers read `/etc/timezone` fresh on each call.

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
(`runtimes/openclaw/mcp.go`). The payload **always wins** — `mcp_url` in the payload
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

#### `channel.refresh_config`

Re-applies a channel's canonical config block on an already-onboarded device — for
older customers whose runtime config predates schema additions (e.g. the Slack
`socketMode` block, object-form streaming, `dmPolicy`). Config-only: **no** plugin
install, CLI bootstrap, or pairing. Credentials are read from `config.json` on the
device — they are **NOT** carried in the payload; the device builds the per-channel
`RefreshChannelRequest` from config.json.

**Generic.** Refresh now works for `telegram`, `slack`, and `discord` — previously only
`slack` was wired, and other channels returned `channel_not_supported`. The capability
gate still applies: a channel the **active** runtime can't run returns
`channel_not_supported`.

**Receive:** `{"cmd": "data", "kind": "channel.refresh_config", "data": {"channel": "slack"}}`

**Async flow** — the device acks `configuring` (not `starting`, because the channel was
already set up; this is a re-apply), then runs the write + gateway restart in the
background and publishes a terminal status:

```json
{
  "device": "lamp",
  "type": "data",
  "kind": "channel.refresh_config",
  "status": "configuring | success | failure",
  "error": "<code>",
  "data": { "channel": "slack", "runtime": "2026.6.10" }
}
```

`data.runtime` carries the detected runtime version string (empty when probing failed)
so the backend can correlate refresh outcomes with runtime upgrades. Error codes (in
`error` when `status:"failure"`, mapped from sentinels via `errors.Is`):

| Code | Meaning |
|------|---------|
| `slack_credentials_missing` | config.json has no credentials for the channel being refreshed (kept for wire back-compat; applies to any channel, not just slack) |
| `channel_not_supported` | the active runtime can't run this channel |

### `ota` — Trigger OTA update

Handled by bootstrap worker, not through MQTT handler directly.

## Code

| File | Role |
|------|------|
| `system/lib/mqtt/client.go` | MQTT client (connect, subscribe, publish) |
| `system/lib/mqtt/config.go` | Config struct |
| `system/lib/mqtt/options.go` | Connection options |
| `system/lib/mqtt/factory.go` | Factory to create client with unique ID |
| `system/server/device/delivery/mqtt/handler.go` | Command dispatcher |
| `system/server/device/delivery/mqtt/info_handler.go` | Handle `info` command |
| `system/server/device/delivery/mqtt/add_channel_hander.go` | Handle `add_channel` command (streams pairing events for WhatsApp) |
| `system/server/device/delivery/mqtt/slack_event_handler.go` | Handle `slack_event` / `slack_command` (runtime-aware: forwards Slack HTTP-mode events/slash commands to the local OpenClaw gateway, or drives a hermes turn when the runtime is a `SlackBridge`) |
| `system/server/device/delivery/mqtt/discord_event_handler.go` | Handle `discord_event` (managed shared-bot: dedup, type-assert `domain.DiscordBridge`, inject turn; ack mirrors `publishSlackResult`) |
| `system/server/device/delivery/mqtt/channel_relay.go` | `mqttChannelRelay` — os-server `domain.ChannelRelay`: publishes `discord_reply` / `discord_typing` on fd_channel; installed on the gateway via `DiscordBridge.SetChannelRelay` |
| `system/domain/discord_bridge.go` | Managed-Discord contract: `DiscordInbound`, `ChannelRelay`, `DiscordBridge`, `DiscordReplyDeliverer` |
| `system/server/device/delivery/mqtt/data_handler.go` | Handle `data` command kinds `oauth.set`/`oauth.remove` (+ access-token store) |
| `system/server/device/delivery/mqtt/connector_handler.go` | Handle `connector.set.<code>`/`connector.remove.<code>` (async, writer dispatch via `connectorWriterFor`) |
| `system/server/device/delivery/mqtt/connector_writer.go` | `ConnectorWriter` interface + shared `<code>_access_tokens.json` file helpers |
| `system/server/device/delivery/mqtt/connector_writer_generic.go` | Data-driven `connectorWriter`: payload-driven MCP routing, fallback table, path-traversal guard, per-connector token files |
| `system/server/device/delivery/mqtt/mcp_connector_writer.go` | Special stdio MCP writer (`figma-api`): token file + local-wrapper `openclaw.json` MCP entry |
| `system/server/device/delivery/mqtt/connector_refresh.go` | Connector token refresh loop (`/connector/refresh-token`) |
| `system/server/device/delivery/mqtt/system_info_handler.go` | Handle `data` kinds `system.info`/`system.version`/`system.network` |
| `system/server/device/delivery/mqtt/channel_refresh_handler.go` | Handle `data` kind `channel.refresh_config` (async re-apply of a channel's config block) |
| `system/server/device/delivery/mqtt/timezone_set_handler.go` | Handle `data` kind `timezone.set` (async apply of the device IANA timezone) |
| `system/device/timezone.go` | `SetTimezone`/`CurrentTimezone`: validate zone, rewrite `/etc/localtime` + `/etc/timezone`, best-effort `timedatectl`, persist config |
| `system/device/channels.go` | `RefreshChannelConfig` (generic per-channel request build + capability gate) |
| `system/agent/channel_reconcile.go` | `ChannelReconcile`: re-applies channels after a runtime switch, records `channels_unsupported` |
| `system/server/device/delivery/mqtt/whatsapp_pair_handler.go` | Handle `whatsapp_pair` re-pair command |
| `system/server/device/delivery/mqtt/claudecode_login_handler.go` | Handle `claudecode_login` / `claudecode_login_code` (claude.ai OAuth login) |
| `runtimes/openclaw/pairing.go` | WhatsApp Baileys QR pairing subprocess driver |
| `system/domain/device.go` | MQTTMessage, command constants |
| `system/domain/pairing.go` | PairingEvent + status enum |
