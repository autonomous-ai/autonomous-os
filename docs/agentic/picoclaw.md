# PicoClaw agent backend

PicoClaw is one of the **swappable agentic backends** the os-server can run
behind its agent gateway. The brain is pluggable (CLAUDE.md): os-server talks to
whatever backend `config.agent_runtime` selects through the single
`domain.AgentGateway` interface, so the rest of the pipeline (HAL TTS, `[HW:/…]`
hardware markers, Flow Monitor SSE, sensing drain, Telegram fan-out) never knows
which brain is active.

- **`openclaw`** (default): persistent WebSocket to the OpenClaw daemon. See `docs/os-server.md` + `internal/openclaw`.
- **`hermes`**: HTTP + SSE client against a local Hermes API server. See `docs/agentic/hermes.md` + `internal/hermes`.
- **`picoclaw`**: persistent WebSocket client against a local PicoClaw runtime. This doc. Code: `os/services/internal/picoclaw/`.

> Source of truth is the code. This documents `internal/picoclaw/` as
> implemented; keep it in sync on change (EN: this file, VI: `docs/vi/agentic/picoclaw_vi.md`).

> **Agentic-backend docs:** [`adding-agent-runtime.md`](adding-agent-runtime.md)
> (generic contract + how to add one) · [`hermes.md`](hermes.md) (Hermes) ·
> this file (PicoClaw).
>
> **Status: install parity; client-only gateway.** PicoClaw now ships a device-side
> installer + pre-start hook (`internal/picoclaw/install.sh` + `presync.sh`, embedded
> and registered via `install.go` → `runtimereg`), so a `picoclaw.setup` switch
> installs, provisions, and starts it like hermes (§1.1). Persona/memory migration is
> two-way through the Go reconciler — picoclaw has a `migrate_persona` adapter
> (`runtime_picoclaw.go`), so switching to/from it carries SOUL/IDENTITY/MEMORY/USER/
> KNOWLEDGE both directions; **skills** import on the way IN is done by `picoclaw
> migrate --workspace-only --force` in the presync hook (§1.1). The Go
> gateway itself stays **client-only**: most in-process lifecycle methods
> (`SetupAgent`, `RefreshModelsConfig` …) remain no-ops (§8) because provisioning
> happens out-of-process in install.sh/presync. The exceptions are `EnsureOnboarding`
> (`onboarding.go`, keeps the OS-managed blocks in SOUL/AGENTS/HEARTBEAT current),
> `StartSkillWatcher` (`skill_watcher.go`, CDN skill auto-update), and identity
> (`identity.go`: `WatchIdentity`/`UpdateIdentityName` read/write `IDENTITY.md` like
> OpenClaw) — all real (§1.1, §8).
> Remaining gaps (an emotion-acknowledge hook, queue/steer pinning) are tracked
> against the
> [`adding-agent-runtime.md`](adding-agent-runtime.md) checklist — consult it before
> raising PicoClaw to full parity.

## 1. When and how it is selected

`agent_runtime` in `config.json` picks the backend; resolution lives in
`internal/agent/factory.go` `ProvideGateway()`:

| `agent_runtime` | Backend |
|---|---|
| `"openclaw"` / unset | OpenClaw (default; or `gateway.default` from `DEVICE.md`) |
| `"hermes"` | Hermes (`hermes.ProvideService`) |
| `"picoclaw"` | PicoClaw (`picoclaw.ProvideService`) |
| anything else | OpenClaw (logged as `FALLBACK — unknown runtime=…`) |

On startup `ProvideGateway` prints an `AGENT BACKEND ACTIVE → PICOCLAW` banner
with `ws_url`, `conversation`, and `source`.

## 1.1 Install + provisioning (`install.sh` + `presync.sh`)

A `picoclaw.setup` switch runs the generic `internal/device/switch_runtime.sh`,
which materializes PicoClaw's embedded scripts and drives them. The two scripts
live next to the backend and are embedded + registered in `install.go`:

| Script | On-disk path | Runs |
|---|---|---|
| `install.sh` | `/usr/local/lib/os-runtimes/picoclaw/install.sh` | first switch / failed `verify` |
| `presync.sh` | `/usr/local/bin/runtime-picoclaw-presync` | **before every** picoclaw start (and once at end of install) |

**`install.sh`** (one-time):
1. installs `jq` + `yq` + the pinned `picoclaw` binary (GitHub release,
   `picoclaw-linux-arm64`) into `/usr/local/bin`;
2. `picoclaw onboard` (only when `config.json` is absent) creates `/root/.picoclaw`
   — workspace + a baseline `config.json` and `.security.yml`;
3. writes **`picoclaw.service`** (`ExecStart=/usr/local/bin/picoclaw gateway`,
   `HOME=/root`, `Restart=always`) — `picoclaw gateway` only runs in the foreground,
   so unlike hermes (which ships `gateway install --system`) we wrap it ourselves.
   The unit name equals the runtime name, so **no** `os-runtimes/picoclaw/service`
   declaration file is needed (switch-runtime defaults to it);
4. runs the presync hook once, then drops a `verify` hook (`command -v picoclaw`) so
   switch-runtime can detect + self-heal an orphaned unit.

**`presync.sh`** (every switch — single owner of model + channel config, so it
self-heals after a factory reset, mirroring hermes' presync):
- **§0 migrate** — gated on a sentinel marker `~/.picoclaw/.openclaw-migrated`
  (**not** on `workspace/skills` emptiness — PicoClaw ships built-in skills so that
  dir is always non-empty). When the marker is absent and `/root/.openclaw` exists,
  stop openclaw and run `picoclaw migrate --workspace-only --force` to carry
  persona/memory/skills over from OpenClaw. **`--workspace-only`** means migrate does
  NOT touch `config.json` — converting `openclaw.json` into a picoclaw config produces
  a broken config, so `config.json` stays the valid onboard baseline and §1/§2 assert
  model/channel/gateway on top. It then does the file fixups migrate doesn't: copy
  `HEARTBEAT.md` + `KNOWLEDGE.md` from the
  openclaw workspace (KNOWLEDGE.md is openclaw's living learnings doc, seeded from an
  embedded template then appended daily — migrate skips it), delete `AGENT.md` (so
  PicoClaw runs the legacy `AGENTS.md` path — the only mode that reads `IDENTITY.md`),
  and copy openclaw's `IDENTITY.md` over (migrate skips it too). Finally it writes the
  marker. A factory reset wiping `/root/.picoclaw` clears
  the marker so migrate re-runs; a failed migrate leaves the marker unwritten and
  retries next switch.
- **§0.5 onboarding (`onboarding.go`)** — `EnsureOnboarding`, called on
  boot/config-change like openclaw/hermes, mirrors openclaw's reconcile (trimmed):
  - seeds `KNOWLEDGE.md` from an embedded template (`resources/KNOWLEDGE.md`) **only
    if absent** — covers the fresh picoclaw-only device where presync §0 had no
    openclaw copy; never overwrites;
  - injects the OS-managed `<!-- OS DO NOT REMOVE -->` blocks into `SOUL.md`
    (`ensureSoulMDBlock`, per-device-type soul from DEVICE.md `soul_ref`; owner
    content below `---` preserved), `AGENTS.md` (`ensureAgentsMDBlock`,
    skills/memory/priority rules), and `HEARTBEAT.md` (`ensureHeartbeatMDBlock`,
    daily knowledge-synthesis) — mirroring openclaw but stripped of OpenClaw-only
    content, so the blocks stay current on a plain os-server OTA;
  - **capability-gates skills** (`pruneUnsupportedSkills`): removes skill dirs the
    device can't use — a skill survives if it is supported by `skills.Supported(caps)`
    (the same gate openclaw uses) **or** is a picoclaw built-in
    (`picoclawBuiltinSkills`: `agent-browser`, `github`, `hardware`, `skill-creator`,
    `summarize`, `tmux`, `weather`); everything else under `workspace/skills` is
    deleted. Fail-open when DEVICE.md declares no caps. No reload (skills read per-turn);
  - when any block changed, **restarts the gateway** (`restartPicoclawGateway` →
    `systemctl restart picoclaw`) so it re-reads the workspace files (log+skip when
    systemctl is unavailable). Not the gateway `/reload` endpoint — it needs an admin
    auth we don't hold (the pico channel token is rejected) and isn't confirmed to
    re-read workspace markdown; a restart reliably does.
  - openclaw.json-specific steps (hooks/logging/controlUi registration) are N/A for
    picoclaw's `config.json`; queue/steer pinning is TODO.

A separate **skill watcher** (`skill_watcher.go`, started at boot like openclaw)
polls OTA metadata every 5 min and auto-updates `workspace/skills/<name>` from the
CDN when a supported skill's version bumps (capability-gated via
`skills.Supported`), then notifies the agent with `SendSystemChatMessage`.
- **§1 structure** (`jq` on `config.json`) — `agents.defaults` (provider
  `anthropic-messages`, `model_name "autonomous"`, `restrict_to_workspace:false`,
  `allow_read_outside_workspace:true`), the `autonomous` `model_list` entry, and the
  `channel_list` skeleton. `channel_list.pico` is always enabled.
- **§2 dynamic** (secrets from the **project** `/root/config/config.json`, which
  wins) — `model_list[autonomous].api_base` from `llm_base_url` (PicoClaw needs a
  trailing `/v1`, unlike hermes), `.security.yml` `model_list."autonomous:0".api_keys`
  from `llm_api_key`, the `pico` bearer token (must equal `constants.go` `Token`),
  and each non-pico channel **enabled only when its credentials exist**: telegram
  (`telegram_bot_token` + `telegram_user_id`), discord (`discord_bot_token` +
  `discord_user_id`), slack (`slack_bot_token` + `slack_app_token` + `slack_user_id`),
  whatsapp native (`whatsapp_user_id` → `allow_from`, no token, QR pairing on first
  run). Secrets land in `.security.yml` under `channel_list.<ch>.settings`; structure
  stays in `config.json`.

The running gateway logs confirm the wiring on boot (`Gateway started on
127.0.0.1:18790`, health at `/health` `/ready` `/reload`, `Channels enabled:
[pico]`). A `SECURITY: Channel allows EVERYONE (allow_from is empty) channel=pico`
warning is expected: `pico` is the device-local native gateway and intentionally
has no `allow_from`.

## 2. Wire constants

There is **no per-unit config**; the endpoint is a compile-time constant in
`internal/picoclaw/constants.go`:

| Const | Default | Meaning |
|---|---|---|
| `WSURL` | `ws://127.0.0.1:18790/pico/ws/` | Local PicoClaw WebSocket endpoint |
| `Token` | `darren_pico_token` | Bearer token sent in the `Authorization` header on connect |
| `Conversation` | `device-main` | Default session label until the server assigns a `session_id` |

## 3. Transport

`client.go` holds **one persistent WebSocket** (gorilla/websocket), mirroring the
openclaw reconnect loop but simplified — PicoClaw has **no challenge / pairing
handshake**, just a bearer token:

1. `StartWS` dials `WSURL` with `Authorization: Bearer <Token>`.
2. On connect, readiness flips true (`IsReady`/`ConnectedAt`), the `StateAgentDown`
   LED clears, and a reconnect (not first-connect) plays the i18n reconnect TTS.
3. A keepalive goroutine sends `{"type":"ping","id":…}` every 25s; PicoClaw replies
   `pong` (ignored) which refreshes the 90s read deadline.
4. The read loop translates each inbound frame and dispatches into the registered
   `domain.AgentEventHandler` (synchronously — safe because `FetchChatHistory` is a
   no-op here, so the handler never blocks on a WS RPC).
5. On drop: clear busy + in-flight turn ids, paint `StateAgentDown`, stop servo
   tracking (motion devices only), back off 5s, reconnect.

## 4. Sending a turn

`chat.go` `sendChat` writes one frame and returns immediately (the reply arrives
on the read loop):

```json
{ "type": "message.send", "id": "<reqID>", "payload": { "content": "<text>" }, "session_id": "<if known>" }
```

- Image turns add `payload.attachments: [{ "type": "image", "url": "data:image/jpeg;base64,…" }]` (best-effort; the text content is always sent so the turn proceeds even if the attachment shape is ignored).
- Before the write: mark busy, stash the `runID` as the **pending run id**, record a pending chat trace, and emit `chat_input` / `chat_send` flow events (parity with openclaw).

PicoClaw processes **one turn at a time** and does not stream tokens, so turns
are correlated by a single in-flight `runID` rather than a per-frame id: the
pending run id is adopted by the first inbound frame of the turn.

## 5. Inbound protocol → `domain.WSEvent` mapping

This is the critical part for correct Flow Monitor / web-chat rendering. The
frame `type` alone is **not** enough — `message.create` / `message.update` must
be classified by their payload (`placeholder` / `kind` / `tool_calls` / `content`),
in this priority order (`translator.go` `categorize`):

| Inbound frame | Classified as | Emitted `domain.WSEvent` |
|---|---|---|
| `typing.start` | turn start | `agent` lifecycle `phase:start` (once per turn) |
| `message.create/update`, `placeholder:true` | thinking | *(none — status, not content)* |
| `message.create/update`, `kind:"thought"` / `thought:true` | reasoning | *(none — rendered as status only)* |
| `message.create`, `kind:"tool_calls"` / has `tool_calls` | tool call | `agent` tool `phase:start` + `phase:end` per call |
| `message.create/update`, non-empty `content` (none of the above) | **final answer** | `chat` `state:final role:assistant` **+** `agent` lifecycle `phase:end` (with usage) — **ends the turn** |
| `error` | error | `agent` lifecycle `phase:error` — ends the turn |
| `typing.stop` / `message.delete` / `pong` | — | *(ignored)* |

### Turn lifecycle gotchas

- **`typing.stop` is NOT the end of the turn.** It arrives early, right after the
  thinking phase. The turn ends only on the first **final** frame (or `error`).
- **No-tool turn:** `typing.start → placeholder → typing.stop → message.update (final)`.
  The final is a `message.update` that reuses the placeholder's `message_id`.
- **Tool turn:** `placeholder → typing.stop → message.delete (placeholder removed)
  → message.create kind:"tool_calls" (×N) → message.create (clean, final)`.
- PicoClaw does not emit a separate tool-result frame, so each tool call emits a
  `tool` `phase:start` immediately followed by a `phase:end` with an empty result,
  purely to close the trace.
- `media.create` is defined in the protocol but the server never emits it — media
  rides inside `message.create` as `attachments`.

### Tool call shape

Each entry in `tool_calls` is OpenAI-style: name + params live in
`function.name` and `function.arguments` (a **JSON string**, not an object). The
agent's human-readable lead-in is in `extra_content.tool_feedback_explanation`
(may contain stray ANSI control chars from terminal input). The current
translator forwards `name` + `arguments`; the explanation is logged but not
surfaced (the device `AgentPayload` has no slot for it).

### Token usage

`context_usage` (only on the final frame) is cumulative context size, not
per-turn input/output. It maps to `TokenUsage{ InputTokens: history_tokens,
TotalTokens: used_tokens }`.

## 6. Session

PicoClaw owns the session: the server-assigned `session_id` is captured from any
inbound frame and stored (`SetSessionKey`) so the next `message.send` echoes it.
`NewSession` just clears the local id so the next turn starts a fresh server
session. There is no compact RPC, so `CompactSession` is a no-op.

## 7. Channel capability

PicoClaw runs **telegram only**. The Telegram receive loop is **device-owned**
(driven by `config.TelegramBotToken`), and PicoClaw has no slack/discord delivery
of its own. The three channel methods in `internal/picoclaw/channels.go` encode
this honestly:

| Method | telegram | slack / discord / whatsapp |
|---|---|---|
| `SupportedChannels()` | returns `[telegram]` (the only entry) | — |
| `AddChannel(…)` | honest **no-op success** — telegram is device-owned, so there is nothing to write into the runtime | returns `domain.ErrChannelNotSupported` |
| `RefreshChannelConfig(…)` | `("", nil)` — success no-op (no runtime re-apply needed) | returns `domain.ErrChannelNotSupported` |

This is part of a **repo-wide generic capability model**: every runtime declares
`SupportedChannels()` and returns `domain.ErrChannelNotSupported` (the string
`"channel_not_supported"`) for channels it cannot run, instead of the old silent
no-op. The shared not-supported behavior and the post-switch `ChannelReconcile`
are documented in [`adding-agent-runtime.md`](adding-agent-runtime.md) — see there
rather than duplicating here.

**Switching FROM openclaw → picoclaw:** if openclaw had slack/discord configured,
those channels become unsupported under PicoClaw. After the switch
`ChannelReconcile` reports them in the MQTT info uplink's `unsupported_channels`
field (`domain.MQTTInfoResponse`), and their creds **stay in `config.json`** —
switching back to openclaw restores them.

## 8. What is stubbed

Everything not on the PicoClaw hot path is a no-op so the single
`domain.AgentGateway` interface is satisfied without inventing features the
backend does not have: `SetupAgent`, WhatsApp pairing, `ResetAgent`,
`RefreshModelsConfig`, `FetchChatHistory`, `CompactSession`, MCP entry writes,
the model watchers (`StartModelSync`/`StartPrimaryModelWatch`), `UpdatePrimaryModel`.
(`AddChannel` / `RefreshChannelConfig` are NOT stubs — they return
`domain.ErrChannelNotSupported` for unsupported channels, see §7; `EnsureOnboarding`
(§1.1) and `StartSkillWatcher` (skill auto-update, §1.1) are real.) These are also
real, not stubs: `RestartAgent` (restarts the `picoclaw` systemd unit via
`restartPicoclawGateway`), `GetConfigJSON` (returns `/root/.picoclaw/config.json` —
the structure file; secrets in `.security.yml` are never exposed), and
`WatchIdentity` / `UpdateIdentityName` (`identity.go`) — PicoClaw's `IDENTITY.md` is
a 1-for-1 copy of OpenClaw's, so the `**Name:**` card line is watched (→ wake words)
and rewritten exactly as on OpenClaw.
HAL TTS/voice, Telegram fan-out, sensing-event queue/drain, and the run-marker
helpers (guard / broadcast / web-chat / silent / pose-bucket) are backend-agnostic
and behave exactly like the Hermes backend.

These stay no-ops **on purpose**: PicoClaw is provisioned out-of-process by
`install.sh` + `presync.sh` (§1.1), not by in-process gateway calls. Install,
model/channel config, and persona migration all happen in those scripts during the
`switch-runtime` flow. The one exception is **`EnsureOnboarding`**
(`onboarding.go`), which is real: it injects the OS-managed block into
`workspace/AGENTS.md` on boot/config-change (§1.1), the same contract openclaw has.
