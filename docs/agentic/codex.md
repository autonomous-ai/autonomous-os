# Codex agent backend

Codex is one of the **swappable agentic backends** the os-server can run behind
its agent gateway. The brain is pluggable (CLAUDE.md): os-server talks to
whatever backend `config.agent_runtime` selects through the single
`domain.AgentGateway` interface, so the rest of the pipeline (HAL TTS, `[HW:/…]`
hardware markers, Flow Monitor SSE, sensing drain, Telegram fan-out) never knows
which brain is active.

- **`openclaw`** (default): persistent WebSocket to the OpenClaw daemon. See `docs/os-server.md` + `internal/openclaw`.
- **`hermes`**: HTTP + SSE client against a local Hermes API server. See `docs/agentic/hermes.md` + `internal/hermes`.
- **`picoclaw`**: persistent WebSocket client against a local PicoClaw runtime. See `docs/agentic/picoclaw.md` + `internal/picoclaw`.
- **`codex`**: the **OpenAI Codex CLI** as the device agent brain, behind a local WS bridge. This doc. Code: `os/services/internal/codex/`.

> Source of truth is the code. This documents `internal/codex/` as implemented;
> keep it in sync on change (EN: this file, VI: `docs/vi/agentic/codex_vi.md`).

> **Agentic-backend docs:** [`adding-agent-runtime.md`](adding-agent-runtime.md)
> (generic contract + how to add one) · [`hermes.md`](hermes.md) ·
> [`picoclaw.md`](picoclaw.md) · this file (Codex).
>
> **Status: built, NOT device-verified yet.** The full stack (install, presync,
> gatewayd, translator, MCP, reset) compiles and is unit-tested, but no device
> has run the switch flow end-to-end. Two explicit on-device checks are flagged
> in the code: the gatewayd must listen on `127.0.0.1:18792` with the
> `constants.go` token, and ⚠️ campaign-api must serve `{base}/responses`
> (Codex speaks the Responses API only — see §1.2).

## 1. Overview & how it is selected

The Codex CLI has no server mode of its own, so the device runs a thin local
**WS bridge**: the `codex.service` systemd unit runs **`os-server
codex-gatewayd`** — the bridge is **compiled into the os-server binary**
(`internal/codex/gatewayd`, a Go port of the reference `bridge.py`; **no Python
on the device**). The bridge exposes `ws://127.0.0.1:18792/codex/ws/` (bearer
token `autonomous_codex_token`) and spawns **one subprocess per turn**:

```
codex exec --json --dangerously-bypass-approvals-and-sandbox --cd /root/.codex/workspace
```

resuming the thread id persisted in `/root/.codex/session.json` (`codex exec
resume <id>`). Turns are strictly serialized (buffered queue + single worker).
The permissive flags are deliberate: an appliance running as root must never
block on an approval prompt (paired with `approval_policy = "never"` +
`sandbox_mode = "danger-full-access"` in config.toml, §1.2).

`agent_runtime` in `config.json` picks the backend; resolution lives in
`internal/agent/factory.go` `ProvideGateway()` — `"codex"` →
`codex.ProvideService`, anything unknown falls back to OpenClaw. On startup a
`AGENT BACKEND ACTIVE → CODEX` banner prints `ws_url` + `conversation`.

Wire constants (`internal/codex/constants.go`, no per-unit config):

| Const | Default | Meaning |
|---|---|---|
| `WSURL` | `ws://127.0.0.1:18792/codex/ws/` | Local bridge WebSocket endpoint |
| `Token` | `autonomous_codex_token` | Bearer token on connect; the bridge reads the same value from `/root/.codex/.env` (`CODEX_WS_TOKEN`, presync-owned) |
| `Conversation` | `device-main` | Label only — Codex owns its thread ids (§3) |

## 1.1 Install (`install.sh`)

A `codex.setup` switch runs the generic `internal/device/switch_runtime.sh`,
which materializes Codex's embedded scripts. `install.sh` (one-time, self-
sufficient — a direct `bash install.sh` fully configures AND starts the
backend):

1. prerequisites `jq` + `curl`;
2. installs the Codex CLI from a **pinned GitHub release** (`rust-v0.142.5`,
   asset `codex-aarch64-unknown-linux-musl.tar.gz` — static musl, no runtime
   deps) to `/usr/local/bin/codex`; idempotent (skips when the pinned version
   is already installed);
3. runs the presync hook once (`/usr/local/bin/runtime-codex-presync`,
   materialized by os-server BEFORE the installer — §1.2);
4. writes + enables **`codex.service`** (`ExecStart=/usr/local/bin/os-server
   codex-gatewayd`, `EnvironmentFile=/root/.codex/.env`, `HOME=/root`,
   `Restart=always`) — nothing to materialize for the bridge, it ships inside
   os-server; then drops a cheap offline `verify` hook (`command -v codex` +
   os-server binary present) for switch-runtime self-heal.

Unit name == runtime name (`codex.service`), so no `os-runtimes/codex/service`
declaration file is needed. Install logs go to `/root/.codex/install.log`
(persistent rootfs — `/var/log` is volatile zram on these boards).

## 1.2 Presync (`presync.sh`) — embedded, runs on every switch + every boot

`presync.sh` is embedded in os-server and materialized to
`/usr/local/bin/runtime-codex-presync`. It runs before every codex start
(switch-runtime), once at the end of install, **and on every os-server boot /
config change via `EnsureOnboarding`** (hermes pattern): `EnsureOnboarding`
hashes the presync-owned files (`config.toml` + `.env`) around the run and
restarts the gateway only on a real change. It owns everything stateful:

- **§1 MIGRATE** — one-time persona/memory/skills copy from the openclaw
  workspace, gated on the marker `/root/.codex/.openclaw-migrated`. Stops
  openclaw first (3 retries, non-fatal), then copies `IDENTITY.md`, `SOUL.md`,
  `KNOWLEDGE.md`, `HEARTBEAT.md`, `MEMORY.md`, `USER.md` **and `AGENTS.md`**
  verbatim (Codex reads `AGENTS.md` natively — zero-translation persona slot;
  the Go onboarding re-injects the OS block anyway), plus `memory/` + `skills/`
  only when absent. The marker is written only after a clean copy, so a failed
  migrate retries next run; a factory reset wiping `/root/.codex` clears it so
  migrate re-runs on the next switch.
- **§2 CONFIG** — regenerates the head of `/root/.codex/config.toml` from
  config.json: `model` from `llm_model` (fallback `Auto-AI`),
  `model_provider = "autonomous"` → `[model_providers.autonomous]` with
  `base_url` from `llm_base_url` normalized to end in `/v1` (Codex appends
  `/responses` itself), `env_key = "OPENAI_API_KEY"`, and
  **`wire_api = "responses"` ONLY** — the chat-completions wire was removed
  upstream ~2/2026 (⚠️ VERIFY ON DEVICE: campaign-api must serve
  `{base}/responses`). Also `approval_policy = "never"` +
  `sandbox_mode = "danger-full-access"`. The **tail from the first
  `[mcp_servers` line onward is preserved verbatim** — os-server's `mcp.go`
  owns those entries (§7), so the two owners never collide.
- **§3 ENV** — writes `/root/.codex/.env` (systemd EnvironmentFile, mode 0600):
  `CODEX_WS_TOKEN` (must equal `constants.go` `Token`), `CODEX_PORT=18792`,
  `CODEX_HOME=/root/.codex`, `CODEX_WORKSPACE=/root/.codex/workspace`, and
  `OPENAI_API_KEY` from `llm_api_key`.

On top of the presync run, `EnsureOnboarding` (`onboarding.go`) does the same
workspace reconcile the other backends get: seeds `KNOWLEDGE.md` from the
embedded template only if absent, injects the OS-managed
`<!-- OS DO NOT REMOVE -->` blocks into `SOUL.md` / `AGENTS.md` /
`HEARTBEAT.md` (OpenClaw-derived, stripped of OpenClaw-only bits),
capability-gates skills, and restarts the gateway when a block changed.

## 2. Transport & sending a turn

`client.go` holds one persistent WebSocket to the bridge (picoclaw-shaped:
bearer token, no pairing handshake, 25s ping keepalive → `pong`, reconnect
with backoff, `StateAgentDown` LED on drop). `chat.go` `sendChat` writes one
frame and returns; the reply arrives on the read loop:

```json
{ "type": "message.send", "id": "<reqID>", "payload": { "content": "<text>",
  "attachments": [{ "type": "image", "url": "data:image/jpeg;base64,…" }] } }
```

The bridge saves attachments to `/root/.codex/attachments` and passes them via
`codex exec -i <path>`. A `{"type":"session.new"}` frame makes the bridge drop
the persisted thread id (§4). Codex processes one turn at a time and does not
stream tokens, so turns are correlated by a single in-flight `runID` (the
pending run id is adopted by the first inbound frame of the turn).

## 3. Event translation (`translator.go`)

The bridge forwards the `codex exec --json` JSONL events **verbatim** (plus its
own `bridge.status` / `bridge.error` / `pong` frames); the Go translator maps
them onto the same `domain.WSEvent` shape the OpenClaw handler consumes:

| Inbound event | Emitted `domain.WSEvent` |
|---|---|
| `thread.started` | capture thread id as session key + `agent` lifecycle `phase:start` (once per turn) |
| `turn.started` | `agent` lifecycle `phase:start` (once per turn) |
| `item.started` `command_execution` / `mcp_tool_call` | `agent` tool `phase:start` (`shell` / `server.tool`) |
| `item.completed` `command_execution` / `mcp_tool_call` | tool `phase:end` (start emitted first when unseen) |
| `item.completed` `web_search` / `file_change` | tool `phase:start` + `phase:end` pair |
| `item.completed` `agent_message` | **accumulated** — no delta stream in exec mode |
| `item.*` `reasoning` / `todo_list` | *(ignored — status, not content)* |
| `turn.completed` | `agent` `stream:assistant` (whole reply as **one** delta) **+** `chat` `state:final role:assistant` **+** lifecycle `phase:end` with usage — ends the turn |
| `turn.failed` / `error` / `bridge.error` | `agent` lifecycle `phase:error` — ends the turn |
| `bridge.status` / `pong` | *(logged / ignored)* |

Like PicoClaw, the accumulated `agent_message` text is surfaced at
`turn.completed` as a single assistant delta **before** `chat.final` /
`lifecycle.end` — the N=1 case of the streaming contract, which is what lets
the shared consumer flush TTS + `[HW:/…]` hardware markers at `lifecycle.end`.

**Usage:** `turn.completed` carries `{input_tokens, cached_input_tokens,
output_tokens}`; the translator maps `input + cached → InputTokens` (an
approximation of the live context size), `output → OutputTokens`,
`TotalTokens = in + out`.

## 4. Session

Codex owns the session: the thread id is captured from the `thread.started`
event and persisted by the bridge in `/root/.codex/session.json`, then replayed
via `codex exec resume <id>` (history lives on disk under
`$CODEX_HOME/sessions/` — process exit ≠ session loss). `NewSession` sends a
`session.new` frame → the bridge drops the thread id → the next turn is fresh
(best-effort when the socket is down: a stale id fails resume and the bridge
retries fresh on its own).

Codex **auto-compacts its own context** (`model_auto_compact_token_limit`), so
`ShouldRotateSession` is only a **150k-token safety net** for runaway threads —
it rarely fires. Per [`adding-agent-runtime.md`](adding-agent-runtime.md) §4
"No fake success", `CompactSession`, `GetConfigJSON` (Codex config is TOML +
`.env` secrets — no JSON file to expose), `UpdatePrimaryModel`, and
`RefreshModelsConfig` all return `domain.ErrNotSupportedByRuntime` — never
`nil`. This is not a dead end: `RefreshModelsConfig`'s caller falls back to
`EnsureOnboarding`, whose presync re-reads `llm_*` from config.json and the
hash gate restarts the gateway — so an llm change **is applied live**, just not
by that method.

## 5. Channels

Codex runs **telegram only**, device-owned — the same model as PicoClaw: the
receive loop is driven by `config.TelegramBotToken`, so `AddChannel(telegram)`
is an honest no-op success and `RefreshChannelConfig(telegram)` returns
`("", nil)`. Slack / discord / whatsapp return `domain.ErrChannelNotSupported`;
after a switch, `ChannelReconcile` reports them as `unsupported_channels` in
the MQTT info uplink and their creds stay in config.json (switching back to
openclaw restores them). See [`adding-agent-runtime.md`](adding-agent-runtime.md).

## 6. Hooks

Codex ships no hooks loader, so OpenClaw's `emotion-acknowledge` hook is
reproduced **natively in Go** (`internal/codex/emotion_ack.go`, mirroring
hermes' `emotion_ack.go`): on each user-visible turn, sendChat fires
`{emotion:"thinking"}` to HAL — same skip prefixes, same intensity, same
capability gate (`skills.SupportedHooks`) as the TS handler. The companion
`turn-gate` hook is intentionally not mirrored (sendChat already marks the turn
busy). ⚠️ Keep it in lockstep with `hooks/emotion-acknowledge/handler.ts`.

## 7. MCP connectors (`mcp.go`)

`WriteMCPEntry` / `RemoveMCPEntry` (the `connector.set` MQTT flow) edit
`/root/.codex/config.toml` `[mcp_servers.<name>]` via **go-toml/v2**, atomically
(temp + rename) under `mcpMu`, then restart the gateway so the next `codex
exec` picks the server up. Shape translation from the canonical
OpenClaw-shaped entry: **http entries map `headers` → `http_headers` and the
`type` key is dropped** (Codex infers the transport from `url` vs `command`);
stdio entries pass through. `RemoveMCPEntry` is idempotent (`removed=false`, no
restart, when absent). presync only regenerates the config **head** and
preserves the `[mcp_servers` tail (§1.2), so the entries survive every sync.

## 8. Factory reset (`reset.go`)

`ResetAgent` (called by `server/system/factoryreset.go` on the active gateway)
preserves nothing — config.toml/.env are regenerated by presync on the next
switch: **stop** `codex.service` (+ verify inactive, 5s poll), **disable** it
(reboot defaults to openclaw), **wipe `/root/.codex` wholesale** — config, CLI
auth, threads (`sessions/`), workspace, and the `.openclaw-migrated` marker (so
presync §1 re-migrates on the next switch) — then recreate the baseline
`workspace/` + `attachments/` dirs (Codex has no onboard subcommand; the CLI
recreates its own state under `CODEX_HOME` on first run).

## 9. Auth — phase 2 note

Phase 1 (current) authenticates with an **API key via campaign-api**:
`OPENAI_API_KEY` = config.json `llm_api_key`, provider `base_url` =
`llm_base_url` (§1.2). ChatGPT-subscription auth (`codex login --device-auth`)
is **deferred to phase 2** — it will share the login-pairing plumbing with the
claudecode branch's `ClaudeLoginPairer` once that branch merges.
