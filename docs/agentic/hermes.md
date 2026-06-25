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
> keep it in sync on change (EN: this file, VI: `docs/vi/agentic/hermes_vi.md`).

> **Agentic-backend docs:** [`adding-agent-runtime.md`](adding-agent-runtime.md)
> (generic contract + how to add one) · this file (Hermes) ·
> [`picoclaw.md`](picoclaw.md) (PicoClaw). Generic switch/install/migration
> mechanics live in the first; per-backend protocol lives in the others.

## 1. When and how it is selected

`agent_runtime` in `config.json` picks the backend; resolution lives in
`internal/agent/factory.go` `ProvideGateway()`:

| `agent_runtime` | Backend |
|---|---|
| unset | falls back to `gateway.default` in `devices/<type>/DEVICE.md`, then OpenClaw if that is empty too |
| `"openclaw"` | OpenClaw (default) |
| `"hermes"` | Hermes (`hermes.ProvideService`) |
| `"picoclaw"` | PicoClaw (`picoclaw.ProvideService`) — persistent WebSocket client; assumes the PicoClaw service is already running. See `docs/agentic/picoclaw.md` + `internal/picoclaw`. |
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

## 8. Channels (Telegram/Slack/Discord) — inbound visibility + fan-out

The Hermes gateway **owns all messaging-channel I/O**: it polls Telegram (and
Slack/Discord/WhatsApp) itself with the tokens `presync` syncs into
`~/.hermes/.env`, runs the turn, and replies to the chat directly. os-server is
not on that path, so — unlike OpenClaw, which pushes `session.message` WS events —
a channel turn under Hermes would never show up in Flow Monitor. The gateway has
no cross-platform turn broadcast to subscribe to either; the only seam is its
**hook** system.

So os-server installs a gateway hook, `os-server-observer`
(`internal/hermes/hooks/os-server-observer/{HOOK.yaml,handler.py}`, materialized
to `~/.hermes/hooks/` by `ensureObserverHook` on every boot — see §10). It fires
on `agent:start` / `agent:end` for **every** platform and POSTs the turn to the
loopback endpoint `POST /api/agent/channel-turn` (`handler_channel_turn.go`),
which emits the same flow events a normal turn does:

- `agent:start` → `chat_input` (source `channel`, with `sender` + `channel`) plus
  `lifecycle_start`.
- `agent:end` → `lifecycle_end` plus `tts_suppressed` carrying the reply text
  (the reply went to the channel, not the device speaker — the same node the
  OpenClaw channel path uses, so the web turn renders it), or `no_reply` for an
  empty / `NO_REPLY` turn.

Both events share one `run_id`, correlated by `session_id`. The handler is
channel-agnostic (keyed on the `platform` field) and **skips** `api_server` / `cli`
turns — those are os-server's own `/v1/responses` calls, already logged by
`sendChat`; emitting them again would double the device-originated turns.

Outbound (proactive) sends — `Broadcast` / `SendToUser` in `telegram.go` /
`telegram_sender.go` — go straight to the Telegram Bot API for device-initiated
alerts, using the bot token and the `telegramTargetsFile` chat list.

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
`/usr/local/bin/hermes` **stage by stage** (see below), stops `openclaw` (so the
skill import doesn't race its running state), seeds the `API_SERVER_*` keys in
`~/.hermes/.env`, then **delegates all `config.yaml` + skill setup to the presync
hook**, which it invokes inline, and finally installs + starts the gateway as a
**system service** via `hermes gateway install --system --run-as-user root` +
`hermes gateway start --system` (unit: **`hermes-gateway.service`**). Because the
presync hook does the config + skills (see below) and the installer runs it
inline, a direct `bash install.sh` is fully configured and running.

> **Staged CLI install (skips `node-deps`).** Rather than the monolithic
> `curl | bash --skip-setup`, the installer downloads the upstream installer
> (`https://hermes-agent.nousresearch.com/install.sh`) to a temp file and drives
> only these stages, in order:
> `prerequisites repository venv python-deps path config`
> (each via `bash <installer> --stage <name> --non-interactive`). It deliberately
> **skips the `node-deps` stage**: that stage runs an `npm install` of
> browser-tool native modules (node-gyp) that hangs indefinitely on the ARM board,
> and a voice device never uses browser tools — the gateway is Python-only and
> doesn't need them. After the loop it stamps
> `echo git > /usr/local/lib/hermes-agent/.install_method` so a later
> `hermes update` recognizes this as a git install.

> **Install log lives off zram.** The installer tees all stdout+stderr to
> `$HERMES_LOG`, default **`/root/.hermes/install.log`** (persistent rootfs) —
> **not** under `/var/log`, which on these boards is a volatile zram mount
> (log2ram) wiped on reboot, exactly losing the install log when you need it.
> Follow it live with `tail -f /root/.hermes/install.log`; override the path with
> `HERMES_LOG=…` in the environment before invoking.

> Unit name: the gateway runs as `hermes-gateway.service`. The installer declares
> this in `/usr/local/lib/os-runtimes/hermes/service` so `switch-runtime` enables
> the right unit (§11); `reset_hermes.go` targets the same unit.

### The presync hook owns `config.yaml` + skills

The Hermes model config in `config.yaml` and the OpenClaw-imported skills are owned
by the **presync hook** (`internal/hermes/presync.sh`), **not** by `install.sh`.
**os-server materializes the hook to `/usr/local/bin/runtime-hermes-presync` on
every switch** (`materializePresync`, registered via `runtimereg.RegisterPresync`),
so a plain os-server OTA refreshes it on disk — unlike a copy written once by
`install.sh`, which `switch-runtime` skips on a later switch (the *activation gap*;
see `docs/agentic/adding-agent-runtime.md` §3).

**The hook also runs on every os-server boot AND at initial setup**, not only on a
switch — both via `EnsureOnboarding` (`internal/hermes/onboarding.go`), which
executes the embedded `PresyncScript` and restarts `hermes-gateway` only when
`config.yaml` actually changed (content-hash guarded — no restart loop):

- **Boot:** the startup sequence calls `EnsureOnboarding`. Closes the gap where a
  device that **boots straight into Hermes** (`DEVICE.md gateway.default: hermes`,
  or imaged with it) without ever switching from OpenClaw, or whose `llm_*` changed
  while Hermes was already active, would keep a stale `config.yaml` that never
  picked up `config.json`'s real `llm_api_key`/`base_url`.
- **Setup:** `SetupAgent` (also in `onboarding.go`) just calls `EnsureOnboarding`.
  This works because **Hermes provisions from `config.json`, not from the
  `SetupRequest`** (unlike OpenClaw, whose `SetupAgent` writes `openclaw.json`
  straight from the request — hence OpenClaw needs *two* distinct functions, Hermes
  *one*). The device setup flow saves `config.json` **before** calling `SetupAgent`
  (`internal/device/service.go` — the call was deliberately ordered after
  `config.Save()`), so presync materializes `config.yaml`/`.env` from the
  freshly-entered keys immediately instead of waiting for the next boot.

This gives Hermes the same config self-heal OpenClaw has (`ensureAgentDefaults` +
`StartModelSync`), reusing the one presync script instead of duplicating the sync
in Go. (A live `llm_*` rotation via `PUT /api/device/config` without a reboot still
waits for the next boot — a config-change trigger is a possible follow-up.)

The hook runs right before the gateway starts (on switch and boot, and inline
during install) and does three things, in order:

1. **Restores skills** — when `~/.hermes/skills/openclaw-imports` is empty (first
   install OR after a factory reset wiped it), runs `hermes claw migrate` (it
   **copies** OpenClaw skills, no transform). Guarded on the dir being empty so a
   normal switch is a no-op (no re-import churn). `claw migrate` also touches
   SOUL/MEMORY, but harmlessly: the Go persona migration (§12) runs afterwards and
   rewrites those cleanly, so only the skills persist.
2. **Ensures the `config.yaml` model structure** (idempotent — self-heals after a
   factory reset's `hermes setup --reset` blanks it). It coerces a reset-left
   `model: ''` back to a map, then asserts:
   - `.model.provider = custom:autonomous`
   - `.model.default = "Auto-AI"` — the **fixed** campaign-api model alias. os-server
     sends a fixed request model (`constants.go` `Model`) per turn, so this is **not**
     taken from `llm_model` (that is OpenClaw's primary model, irrelevant to Hermes).
   - `.custom_providers[0]` → `name: autonomous`, `key_env: AUTONOMOUS_API_KEY`,
     `api_mode: anthropic_messages`, `base_url` (default campaign-api, overridden below).
3. **Syncs per-device values** from `config.json` (only non-empty fields, so
   unconfigured channels are untouched):

| `config.json` | → | Hermes |
|---|---|---|
| `llm_base_url` | → | `config.yaml` `.custom_providers[0].base_url` |
| `llm_api_key` | → | `.env` `AUTONOMOUS_API_KEY` |
| `telegram_bot_token` | → | `.env` `TELEGRAM_BOT_TOKEN` |
| `telegram_user_id` | → | `.env` `TELEGRAM_ALLOWED_USERS` |
| `slack_bot_token` / `slack_app_token` / `slack_user_id` | → | `.env` `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` / `SLACK_ALLOWED_USERS` |
| `discord_bot_token` / `discord_guild_id` / `discord_user_id` | → | `.env` `DISCORD_BOT_TOKEN` / `DISCORD_GUILD_ID` / `DISCORD_ALLOWED_USERS` |
| `whatsapp_user_id` | → | `.env` `WHATSAPP_ALLOWED_USERS` |

`.env` `API_SERVER_KEY` must equal `constants.go` `APIKey` (`hermes-api-key`) or
every turn 401s. Hermes must listen on `127.0.0.1:8642` to match `BaseURL`.

To target a different Hermes endpoint / key / model today, edit
`internal/hermes/constants.go` and rebuild (making these per-unit configurable is
future work).

## 11. Switching backends at runtime

The switch mechanism is **generic** (backend-agnostic) and fully documented in
[`adding-agent-runtime.md`](adding-agent-runtime.md) §2–§3: three triggers (MQTT
`hermes.setup`, HTTP `POST /api/device/agent-runtime {"runtime":"hermes"}`, web
Settings → *Runtime*) funnel into `device.Service.UpdateAgentRuntime`, which runs
`switch-runtime <new> <old>` under `systemd-run --wait` and persists
`config.agent_runtime` only after a clean exit (so a mid-switch crash resolves the
still-installed old backend). Hermes-specific facts the generic switcher relies on:

- **Unit name** `hermes-gateway.service` (not `hermes.service`) — declared in
  `/usr/local/lib/os-runtimes/hermes/service` so `switch-runtime` enables the right
  unit; `reset_hermes.go` targets the same unit.
- **Verify hook** `/usr/local/lib/os-runtimes/hermes/verify` runs `command -v
  hermes` (cheap CLI-presence check). It is deliberately **not** a config-structure
  check — config self-heals via presync (§10), so a verify failure would force an
  unnecessary full reinstall.
- **Presync** `runtime-hermes-presync` runs before the gateway starts (§10).
- The MQTT `hermes.setup` ack reflects the **real** outcome (success only after the
  switch lands; failure with the rollback reason otherwise), since
  `UpdateAgentRuntime` blocks on the switcher's exit code.

Confirm the swap from the `AGENT BACKEND ACTIVE → HERMES` banner + a healthy
`/health` poll.

## 12. Persona, memory & skills carried across a switch

Switching openclaw→hermes runs a Go persona migration
(`internal/agent/migrate_persona/openclaw_to_hermes.go`) at os-server boot —
**separate from `claw migrate`**. It carries, into `~/.hermes/`:

- **SOUL.md** (rebranded) — and, because Hermes has no separate IDENTITY.md slot,
  inlines the owner's filled IDENTITY fields as a `## Your identity card` block so
  the custom name (e.g. "Ngân") survives. `UpdateIdentityName` (device rename) edits
  that block; `WatchIdentity` (`internal/hermes/identity.go`) polls SOUL.md and, on
  a name change, pushes the new wake words to HAL + `i18n.SetDeviceName` — mirroring
  OpenClaw's `WatchIdentity`, just watching SOUL.md instead of IDENTITY.md.
- **MEMORY.md + daily `memory/*.md` + KNOWLEDGE.md** → merged into
  `memories/MEMORY.md`. Hermes loads only `MEMORY.md` + `USER.md` **by name** (no
  `memories/*.md` glob), so KNOWLEDGE is folded in rather than kept as a separate,
  ignored file.
- **USER.md** → `memories/USER.md`.

The soul copy uses `Overwrite=true` (a switch adopts the source runtime's persona;
backed up first). The reverse hermes→openclaw **strips the identity card from the
SOUL and restores its fields back into OpenClaw's `IDENTITY.md`** (`restoreIdentityCard`,
the inverse of the inline) — so the name set under Hermes survives the trip back,
not just the trip out. **Skills** stay fresh under Hermes via
`internal/hermes/skill_watcher.go` — CDN auto-update into `skills/openclaw-imports`,
capability-gated, mirroring the OpenClaw watcher (shared engine in
`internal/skills/skillzip.go`).

### Round-trip is content-lossless but structurally one-way (Hermes-specific)

Persona, name, user profile, and memory **content** survive openclaw→hermes→openclaw
without loss. The one **structural** asymmetry is a consequence of Hermes loading
only `MEMORY.md` + `USER.md` by name (no `KNOWLEDGE.md`, no daily-memory slot):

- The forward step **folds** OpenClaw's `KNOWLEDGE.md` and daily `memory/*.md` **into**
  the single Hermes `MEMORY.md`. On the way back those entries are already merged, so
  they all land in OpenClaw's `MEMORY.md` — **never split back out** into a
  `KNOWLEDGE.md` or per-day files. No data is lost; the structure is flattened.

This is specific to Hermes's memory model — a backend that *does* have those slots
would map them 1:1 and round-trip cleanly. (See the fold-vs-move rule in
[`adding-agent-runtime.md`](adding-agent-runtime.md) §4.)

> **Adding another backend** is a generic recipe — see
> [`adding-agent-runtime.md`](adding-agent-runtime.md) for the `AgentGateway`
> contract, the install/presync pattern, migration, skills, hooks, reset, and the
> full checklist.
