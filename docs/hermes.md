# Hermes agent backend

Hermes is one of the **swappable agentic backends** the os-server can run behind
its agent gateway. The brain is pluggable (CLAUDE.md): os-server talks to
whatever backend `config.agent_runtime` selects through the single
`domain.AgentGateway` interface, so the rest of the pipeline (HAL TTS, `[HW:/…]`
hardware markers, Flow Monitor SSE, sensing drain, Telegram fan-out) never knows
which brain is active.

- **`openclaw`** (default): persistent WebSocket to the OpenClaw daemon. See `docs/os-server.md` + `internal/openclaw`.
- **`hermes`**: HTTP + SSE client against a local Hermes API server (OpenAI *Responses API* style). This doc. Code: `os/services/internal/hermes/`.

> Source of truth is the code. This documents `internal/hermes/` as implemented;
> keep it in sync on change (EN: this file, VI: `docs/vi/hermes_vi.md`).

## 1. When and how it is selected

`agent_runtime` in `config.json` picks the backend; resolution lives in
`internal/agent/factory.go` `ProvideGateway()`:

| `agent_runtime` | Backend |
|---|---|
| unset | falls back to `gateway.default` in `devices/<type>/DEVICE.md`, then OpenClaw if that is empty too |
| `"openclaw"` | OpenClaw (default) |
| `"hermes"` | Hermes (`hermes.ProvideService`) |
| `"picoclaw"` | accepted as a valid runtime (the `picoclaw.setup` switch + `switch-runtime` install it), but has **no `factory.go` case yet** — os-server's gateway client currently falls back to OpenClaw. Wiring `internal/picoclaw` + a resolver case is the remaining "adding a new backend" work (§11). |
| anything else | OpenClaw (logged as `FALLBACK — unknown runtime=…`) |

When `agent_runtime` is unset in `config.json`, the backend is taken from the
device's declared `gateway.default` (`devices/<type>/DEVICE.md`); OpenClaw is used
only if that is also empty. The banner logs `source` so you can tell which won.

On startup `ProvideGateway` prints an `AGENT BACKEND ACTIVE → HERMES` banner with
`base_url`, `conversation`, `model`, and `api_key_set`. There is **no per-unit
config** for these yet — they are compile-time constants in
`internal/hermes/constants.go`:

| Const | Default | Meaning |
|---|---|---|
| `BaseURL` | `http://127.0.0.1:8642` | Local Hermes API server |
| `APIKey` | `hermes-api-key` | Bearer for Hermes |
| `Conversation` | `device-main` | Named channel all turns flow into |
| `Model` | `hermes-agent` | Model id sent to Hermes |

Hermes itself is assumed to be already running on the device at `BaseURL` with
all skills provisioned; os-server is only a per-request client.

## 2. What changes vs OpenClaw — and what does not

| | OpenClaw | Hermes |
|---|---|---|
| Transport | one persistent WebSocket | stateless HTTP POST + SSE per turn |
| Connection state | socket up/down | `/health` poller goroutine (`health.go`) drives `ready`/`connectedAt` |
| Session | the socket | server-side UUID via `X-Hermes-Session-Id` header (§3) |
| Downstream pipeline | — | **identical** — Hermes translates SSE → the same `domain.WSEvent` frames |

Because Hermes emits the same `domain.WSEvent` shape that the OpenClaw handler
(`server/agent/delivery/http/handler_events.go`) already consumes, HAL TTS,
`[HW:/…]` marker routing, monitor SSE, the sensing drain, and Telegram fan-out
all stay untouched. `*hermes.Service` satisfies `domain.AgentGateway` in full
(`Name()`="Hermes", `IsReady`, `ConnectedAt`, `AgentUptime`, `IsBusy`/`SetBusy`,
`QueuePendingEvent`, `SendChat*`, `StartWS`, …).

## 3. Session & conversation model

Hermes has no socket, so the "session" is server-side:

- Every response carries the `X-Hermes-Session-Id` response header — one UUID per
  conversation, stable across reconnects. `Service.sessionUUID` shadows it.
- `Conversation` (`device-main`) is the named channel every turn flows into; all
  chat/sensing/Telegram turns share it so the agent keeps one context.
- `Service.lastResponseID` caches the latest `response.id`, used to chain turns
  (Responses-API style continuation).

State is in-memory only (`sessionUUID`, `lastResponseID`, `reqCounter` + the
guard / broadcast / web_chat / pose-bucket run trackers); nothing persists across
an os-server restart.

## 4. Request protocol — `POST /v1/responses`

`client.go` POSTs a `streamRequest` with `stream: true` and reads an SSE stream:

```jsonc
{
  "model": "hermes-agent",
  "conversation": "device-main",
  "stream": true,
  "instructions": "…",        // optional system/role text
  "input": "<text>",           // plain turn …
  "title": "…"                 // optional
}
```

For **vision** turns `input` is a multi-part array instead of a string — Hermes
accepts both shapes:

```jsonc
"input": [{ "role": "user", "content": [
  { "type": "input_text",  "text": "…" },
  { "type": "input_image", "image_url": "data:…" }
]}]
```

## 5. SSE → `domain.WSEvent` translation

The SSE consumer (`client.go`) streams `response.*` events; `translator.go` maps
them into `domain.WSEvent` frames and dispatches them through the handler
registered by `StartWS` — the same path OpenClaw uses. The turn-lifecycle mirror
matches OpenClaw: `activeTurn` flips true on send and false on
`response.completed`; the completed result carries `response.id` (cached as
`lastResponseID`) and the full assistant text for send-and-wait callers.

Sensing/pose markers are stripped before send using the same regexes as OpenClaw
(`[snapshot: …]`, `[pose_bucket: …]`, `[pose_worst: …]`) so the agent never sees
internal hardware markers.

## 6. Connection state & health

No socket means liveness is polled. `health.go` runs a `/health` poller that
flips `ready` / `connectedAt`, derives `agentStartedAt` from
`/health/detailed.uptime_s` when available, and uses `hasConnected` to skip the
"reconnected" TTS chime on the first successful poll. `AgentUptime()` reports the
Hermes process uptime, independent of os-server.

## 7. Busy state & pending sensing events

Identical contract to OpenClaw: while a turn is active (`IsBusy`), passive sensing
events are dropped or buffered (`QueuePendingEvent`, last-write-wins per type) and
replayed when idle, so ambient signals never interrupt an in-flight command.

## 8. Telegram fan-out

`telegram.go` / `telegram_sender.go` route agent replies back to the originating
Telegram chat. `markTelegramOrigin(runID, chatID)` records where a turn came from
and `consumeTelegramOrigin(runID)` reads it back at reply time, so a Telegram-
initiated turn answers in the right chat while still flowing through the shared
pipeline.

## 9. Voice

`hal.go` wires Hermes turns into the HAL voice path (TTS on speak-end, the same
`lib/hal` entry points OpenClaw uses), so spoken interaction works the same
regardless of backend.

## 10. Operating it

Hermes is installed by `os/services/internal/hermes/install.sh` (co-located with
its implementation). The script is **embedded in os-server** (`go:embed`,
registered via `lib/runtimereg`), so it ships + OTA-updates with the binary;
os-server materializes it to `/usr/local/lib/os-runtimes/hermes/install.sh` and
switch-runtime runs that local copy — fully offline, no CDN round-trip. (The CDN
path `${RUNTIMES_BASE_URL}/hermes/install.sh` remains a fallback for backends not
compiled into the binary.) The installer pulls the Hermes CLI to
`/usr/local/bin/hermes`, stops `openclaw` (so the migration does not race its
running state), runs `hermes claw migrate` (skills only), seeds
`~/.hermes/.env`, **patches only `.model` + `.custom_providers` in
`config.yaml`** (via `yq`, preserving anything else the CLI wrote — not a
full-file overwrite), drops the `runtime-hermes-presync` hook (§11) and **runs
it once inline**, then installs + starts the gateway as a **system service** via
`hermes gateway install --system --run-as-user root` + `hermes gateway start
--system` (unit: **`hermes-gateway.service`**). Because presync runs during
install, a direct `bash install.sh` is fully configured and running without
relying on switch-runtime.

> Unit name: the gateway runs as `hermes-gateway.service`. The installer declares
> this in `/usr/local/lib/os-runtimes/hermes/service` so `switch-runtime` enables
> the right unit (§11); `reset_hermes.go` targets the same unit.

`hermes claw migrate` does **not** carry the model config across, so the presync
hook syncs the device's `llm_*` from `config.json` into the Hermes config — once
during install, and again on every later switch:

| `config.json` | → | Hermes |
|---|---|---|
| `llm_model` | → | `config.yaml` `.model.default` |
| `llm_base_url` | → | `config.yaml` `.custom_providers[0].base_url` |
| `llm_api_key` | → | `.env` `AUTONOMOUS_API_KEY` |
| `telegram_bot_token` | → | `.env` `TELEGRAM_BOT_TOKEN` |
| `telegram_user_id` | → | `.env` `TELEGRAM_ALLOWED_USERS` |
| `slack_bot_token` | → | `.env` `SLACK_BOT_TOKEN` |
| `slack_app_token` | → | `.env` `SLACK_APP_TOKEN` |
| `slack_user_id` | → | `.env` `SLACK_ALLOWED_USERS` |
| `discord_bot_token` | → | `.env` `DISCORD_BOT_TOKEN` |
| `discord_guild_id` | → | `.env` `DISCORD_GUILD_ID` |
| `discord_user_id` | → | `.env` `DISCORD_ALLOWED_USERS` |
| `whatsapp_user_id` | → | `.env` `WHATSAPP_ALLOWED_USERS` |

Only non-empty `config.json` fields are written, so unconfigured channels are
left untouched.

`.env` `API_SERVER_KEY` must equal `constants.go` `APIKey` (`hermes-api-key`) or
every turn 401s. Hermes must listen on `127.0.0.1:8642` to match `BaseURL`.

To target a different Hermes endpoint / key / model today, edit
`internal/hermes/constants.go` and rebuild (making these per-unit configurable is
future work).

## 11. Switching backends at runtime

You do not edit `config.json` by hand. Three triggers — **MQTT** `hermes.setup` /
`picoclaw.setup` (the kind itself names the target backend — no `runtime` field;
each maps `hermes.setup → hermes`, `picoclaw.setup → picoclaw`), **HTTP**
`POST /api/device/agent-runtime` (`{"runtime":"hermes"}`), and the **web**
Settings → *Runtime* section — all funnel into one method,
`device.Service.UpdateAgentRuntime` (`internal/device/service.go`). It validates
the runtime, persists `config.agent_runtime`, and launches the switcher in its
own transient systemd unit (`systemd-run`, so the os-server restart at the end
can't kill it mid-flight):

```
switch-runtime <new> <old>
```

`switch-runtime` is **generic and backend-agnostic** — it is embedded in os-server
(`internal/device/switch_runtime.sh` via `go:embed`) and written to
`/usr/local/bin/switch-runtime` on demand, so it is versioned and OTA-updated with
the binary and needs **no imager/setup.sh change ever**. For a target backend `X`
it:

1. resolves `X`'s unit name (default `X.service`, or whatever the installer
   declared in `/usr/local/lib/os-runtimes/X/service` — hermes →
   `hermes-gateway`) and ensures it exists, else runs `X`'s installer — the
   binary-embedded copy at `/usr/local/lib/os-runtimes/X/install.sh` first, else
   `curl ${RUNTIMES_BASE_URL}/X/install.sh | bash` (openclaw is skipped —
   `openclaw.service` is baked by setup.sh);
2. runs the optional `/usr/local/bin/runtime-X-presync` hook (hermes's syncs
   `llm_*`, per §10);
3. `systemctl enable --now <X-unit>`, then stop the old unit with up to 3
   `disable --now <old-unit>` retries (verifying it went inactive between tries);
   after 3 attempts it proceeds regardless so a stuck old runtime never blocks
   the switch;
4. `systemctl restart os-server`, so `factory.go` re-resolves the gateway.

Confirm the swap from the new `AGENT BACKEND ACTIVE → …` banner + a healthy
`/health` poll in the logs.

**Adding a new backend** (claudecode, …) is just an `install.sh` next
to that backend's implementation (`internal/<name>/install.sh`), `go:embed`-ed +
registered in `lib/runtimereg` from the package's `init()` (it must create
`<name>.service`, optionally drop `runtime-<name>-presync`), plus a
`domain.AgentRuntimes` entry for validation + the web dropdown. A backend already
needs a gateway client under `internal/<name>` and a `factory.go` case, so the
embedded installer adds no new coupling — and nothing in the imager, switcher, or
CDN has to change.
