# Codex agent backend

Codex is one of the **swappable agentic backends** the os-server can run behind
its agent gateway. The brain is pluggable (CLAUDE.md): os-server talks to
whatever backend `config.agent_runtime` selects through the single
`domain.AgentGateway` interface, so the rest of the pipeline (HAL TTS, `[HW:/…]`
hardware markers, Flow Monitor SSE, sensing drain, Telegram fan-out) never knows
which brain is active.

- **`openclaw`** (default): persistent WebSocket to the OpenClaw daemon. See `docs/os-server.md` + `runtimes/openclaw`.
- **`hermes`**: HTTP + SSE client against a local Hermes API server. See `docs/agentic/hermes.md` + `runtimes/hermes`.
- **`picoclaw`**: persistent WebSocket client against a local PicoClaw runtime. See `docs/agentic/picoclaw.md` + `runtimes/picoclaw`.
- **`codex`**: the **OpenAI Codex CLI** as the device agent brain, behind a local WS bridge. This doc. Code: `runtimes/codex/`.

> Source of truth is the code. This documents `runtimes/codex/` as implemented;
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
(`runtimes/codex/gatewayd`, a Go port of the reference `bridge.py`; **no Python
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
`system/agent/factory.go` `ProvideGateway()` — `"codex"` →
`codex.ProvideService`, anything unknown falls back to OpenClaw. On startup a
`AGENT BACKEND ACTIVE → CODEX` banner prints `ws_url` + `conversation`.

Wire constants (`runtimes/codex/constants.go`, no per-unit config):

| Const | Default | Meaning |
|---|---|---|
| `WSURL` | `ws://127.0.0.1:18792/codex/ws/` | Local bridge WebSocket endpoint |
| `Token` | `autonomous_codex_token` | Bearer token on connect; the bridge reads the same value from `/root/.codex/.env` (`CODEX_WS_TOKEN`, presync-owned) |
| `Conversation` | `device-main` | Label only — Codex owns its thread ids (§3) |

## 1.1 Install (`install.sh`)

A `codex.setup` switch runs the generic `system/device/switch_runtime.sh`,
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
  config.json. **Auth gate:** when `/root/.codex/auth.json` exists
  (ChatGPT-subscription login, §9) the head is written WITHOUT `model` /
  `model_provider` / `[model_providers.autonomous]` — codex uses its built-in
  default provider + model (keeping only `approval_policy` + `sandbox_mode`,
  and still preserving the `[mcp_servers` tail). Otherwise (api-key mode):
  `model` from `llm_model` (fallback `Auto-AI`),
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
  `OPENAI_API_KEY` from `llm_api_key` — **omitted in subscription mode** (an
  API key outranks/conflicts with ChatGPT auth).

On top of the presync run, `EnsureOnboarding` (`onboarding.go`) does the same
workspace reconcile the other backends get: seeds `KNOWLEDGE.md` from the
embedded template only if absent, injects the OS-managed
`<!-- OS DO NOT REMOVE -->` blocks into `SOUL.md` / `AGENTS.md` /
`HEARTBEAT.md` (OpenClaw-derived, stripped of OpenClaw-only bits), refreshes
the **global** user AGENTS.md block (`ensureUserAgentsMDBlock`, see below), and
capability-gates skills. Markdown-only changes never restart the gateway —
each `codex exec` re-reads the workspace; only a presync config change or a
unit self-heal restarts it.

**Persona inline block (AGENTS.md).** Codex auto-loads ONLY `AGENTS.md` into
context; the "Session Startup" instruction to read `SOUL.md`/`IDENTITY.md` is
voluntary, and on short turns the model skips it (device-verified: "bạn tên
gì" → "Tôi là Codex"). OpenClaw/Hermes inject the soul into the system prompt
at the runtime layer; codex has no such layer, so `ensurePersonaInlineBlock`
inlines the persona INTO `AGENTS.md`: a
`<!-- OS PERSONA INLINE — DO NOT EDIT (generated from SOUL.md + IDENTITY.md) -->`
… `<!-- /OS PERSONA INLINE -->` block upserted idempotently at the very top
(above the OS mandatory block) containing a mandatory "Who you are" preamble,
the agent name parsed from `IDENTITY.md` (`- **Name:** …`), and the
freshly-reconciled `SOUL.md` verbatim (capped at 20 000 bytes with a
truncation note so `AGENTS.md` stays under codex's 32 KiB project-doc cap).
The block is rebuilt after `ensureSoulMDBlock` on every `EnsureOnboarding`
and also right after a rename (`UpdateIdentityName`), so the very next turn
sees the new name; a missing `SOUL.md` removes the block. Written atomically
(tmp+rename), and only when the bytes actually differ.

### Skills — native `$CODEX_HOME/skills` discovery (`codexSkillsDir`)

Device skills live in **`/root/.codex/skills/<name>/SKILL.md`** — codex-cli's
**native** discovery root (`$CODEX_HOME/skills` on 0.142.x). Codex auto-discovers
every `<name>/SKILL.md` with valid YAML frontmatter here, in **every** session
regardless of cwd, and lists them in the interactive `@` skill picker. This
mirrors the claudecode fix (skills moved to Claude Code's native
`~/.claude/skills`). They are **not** placed in `workspace/skills`, which codex
never scans — a device with skills there gets an empty `@` picker and no native
skill loading. All producers target `codexSkillsDir`: `presync.sh` §1 (openclaw
migration → `$CODEX_DIR/skills`), `skill_watcher.go` (CDN download + the
`notifySkillChanges` message), and `pruneUnsupportedSkills` (capability gate).
`migrateSkillsToCodexHome` lifts any legacy `workspace/skills` left by an older
os-server into the native root and drops the workspace copy (idempotent);
factory reset wipes all of `/root/.codex`, so the set is re-migrated from
openclaw on the next `EnsureOnboarding`.

**Global user AGENTS.md — device-wide rules for coding sessions.** The workspace
`AGENTS.md` only reaches the **device-chat** session: the gatewayd runs
`codex exec --cd /root/.codex/workspace` (`gatewayd/turn.go`), so codex's
repo-root→cwd AGENTS.md walk finds it. A **Telegram coding session**
(`telegram_coding.go`, §"Telegram remote coding-sessions") runs
`codex exec --cd <folder>` in an arbitrary directory (`/root`, `/root/myapp`, …),
whose walk never reaches the workspace file. Codex additionally loads a **global**
user-instructions file, `$CODEX_HOME/AGENTS.md` = `/root/.codex/AGENTS.md`, in
**every** session regardless of cwd (codex-rs `CodexHomeUserInstructionsProvider`,
merged before the project walk). `ensureUserAgentsMDBlock` (`codexUserAgentsMD`)
injects an OS block there — same marker discipline as the workspace file — carrying
the **Connectors (MANDATORY)** rules and the note that device skills live at the
**absolute** path `/root/.codex/skills/<name>/SKILL.md`. Without it, a coding
session has no idea the device's connectors exist and tells the owner
Gmail/Calendar is "not connected". As a read-by-path fallback (independent of
native discovery), the workspace `AGENTS.md` block and `notifySkillChanges` both
cite skills by that **absolute** path, never relative `skills/…` — which would
resolve under the coding session's `<folder>`. No gateway restart is needed
(per-turn re-read); factory reset wipes all of `/root/.codex`, so the global file
is rebuilt on the next `EnsureOnboarding`.

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

### Telegram (device-owned receive loop)

Telegram is **device-owned** under Codex. The Codex CLI has no channel layer
of its own (unlike PicoClaw, whose runtime binary polls the Telegram Bot API
itself — its presync enables `channel_list.telegram` in PicoClaw's own
config), so os-server runs the inbound receive loop:
`runtimes/codex/telegram_poll.go`, one goroutine started from `StartWS`
(outside its reconnect loop, so it survives WS drops). Because it lives inside
the codex service's lifecycle it runs **only while codex is the active
runtime** — it can never compete with the openclaw/hermes gateway pollers for
`getUpdates` (Telegram 409s concurrent pollers).

The loop long-polls `getUpdates` (50 s window, client timeout 70 s) and reads
`config.TelegramBotToken` / `TelegramUserID` **fresh on every iteration** — no
restart is needed after saving or rotating creds; while the token is empty it
rechecks every 30 s, and HTTP/network errors back off 5 s. A message is
accepted only if it is a non-empty **text** message, in a **private** chat,
and `from.id` equals `TelegramUserID` (string compare after `strconv`);
everything else is skipped at debug level while the offset still advances (no
re-delivery). The next-update offset is persisted atomically
(temp + rename) to `/root/.codex/telegram_offset.json`, and each accepted chat
id is upserted into `/root/.codex/telegram_targets.json` so outbound
`Broadcast` (proactive sensing/guard alerts) reaches the chat.

Each accepted message waits for the agent to go idle (`IsBusy`, 500 ms poll,
ctx-aware), then is injected via `sendChat` with flow source `telegram`, so
`chat_input` / `chat_send` fire as usual and Flow Monitor shows the origin.
The injected turn text is prefixed with sender metadata — exact format
`[telegram] Message from <FirstName LastName> (@username) [id:<numeric>]:\n<text>`,
built by `tgUser.label()` (the `(@username)` part is omitted when absent, the
name falls back to `unknown`) — so the agent knows who is talking and on which
channel, mirroring openclaw's telegram plugin behavior.
The run is marked **silent** (the reply must not hit TTS) and tracked in
`telegramRuns`; at `turn.completed`, `emitFinal` consumes the tracker and DMs
the final text back to the originating chat with `[HW:/...]` hardware markers
and TTS audio tags (`[laugh]`, `[sigh]`, …) stripped (`stripForChannel` in
`hal.go`, mirroring the downstream `hwMarkerRe` and HAL's audio-tag
whitelist). On `turn.failed` the tracker is consumed without a DM so the map
cannot leak.

While the turn runs, a `telegramTypingKeeper` goroutine keeps Telegram's
"typing…" indicator alive: after the turn is injected it fires Bot API
`sendChatAction(typing)` immediately and then every 4 s (the indicator expires
after ~5 s) until the run is consumed — reply DMed by `emitFinal` or the turn
errored via `handleError` — capped at `telegramTypingLifetime` = 10 minutes so
a wedged turn cannot leave the chat "typing…" forever. Sends are best-effort
(errors logged at debug and ignored).

### Slack (HTTP-mode proxy path)

Slack is also **device-owned**, via the HTTP-mode proxy path (modeled on the
hermes bridge, `runtimes/hermes/slack.go`): the public bff-campaign-service
proxy receives Slack Events API deliveries and fans them out over MQTT to the
device's `slack_event` handler
(`server/device/delivery/mqtt/slack_event_handler.go`), which dedups by
`event_id` (in-memory LRU, 5 min TTL) and type-asserts the active gateway to
`domain.SlackBridge`. `CodexService` implements that bridge
(`runtimes/codex/slack.go`), so events route here **only while codex is the
active runtime** — no server-side dispatch code changes, same wiring as
hermes. Socket Mode is not involved; the device never opens a Slack WebSocket.

Event handling mirrors hermes: `url_verification` echoes the challenge; only
`message` / `app_mention` events from a real user pass (bot messages, subtyped
events and empty-user events are ignored — loop guard); a leading bot mention
is stripped; and when `config.SlackUserID` is set, only that user may drive
turns (empty = open, the workspace/app already scopes access). The reply goes
to the existing thread when the message was threaded, else it threads under
the user's message (`thread_ts` fallback = message `ts`).

An accepted message is injected asynchronously (goroutine): it waits for the
agent to go idle (`IsBusy`, 500 ms poll, capped at 2 min — past the cap the
message is dropped, since the MQTT handler already acked and Slack will not
retry), then goes through `sendChat` with flow source `slack` and the
sender-metadata prefix
`[slack] Message from <@U…> [channel:C…]:\n<text>`. The run is marked
**silent** (no TTS) and tracked in `slackRuns` (runID → channel/thread_ts/
message ts); receipt is acknowledged with an **eyes reaction** on the user's
message (best-effort). At `turn.completed`, `emitFinal` consumes the tracker
and posts the reply — `stripForChannel`-cleaned, like telegram — back to the
channel/thread via `chat.postMessage` (`config.SlackBotToken`), clearing the
eyes reaction; on `turn.failed` the tracker is consumed and only the reaction
is cleared (no reply). Unlike hermes there is **no progressive streaming**
(`chat.startStream`/`appendStream`) and no assistant "…is typing" status —
codex exec emits the reply whole, so `StreamSlackDelta` is a no-op and the
final text is posted once; `DeliverSlackReply` (called by the shared agent
handler) is a consume-if-present safety net that is normally a no-op because
`emitFinal` consumes the tracker synchronously before dispatching lifecycle
events.

Requirements (config.json): `slack_bot_token` (`chat.postMessage` +
reactions) and optionally `slack_user_id` (allowlist + proactive `Broadcast`
target for the `SlackSender` channel sender). The HTTP-mode
`slack_signing_secret` is consumed by the public proxy, not on the device —
codex, like the hermes bridge, trusts the authenticated MQTT path.

### Discord (device-owned gateway bot session)

Discord is also **device-owned**: it requires a Gateway WebSocket bot session
(there is no long-poll receive API), so os-server runs one via
[discordgo](https://github.com/bwmarrin/discordgo)
(`runtimes/codex/discord.go`). Like the telegram loop, the session is started
from `StartWS` (`go s.startDiscordBot(ctx)`) and lives inside the codex
service's lifecycle — it runs **only while codex is the active runtime**, so
it can never fight another runtime's bot session for the same token. The loop
reads `config.DiscordBotToken` **fresh on every connect attempt** (empty →
recheck every 30 s; failed open → back off 15 s); once open, discordgo
handles gateway reconnects itself and the session is closed when the gateway
ctx ends. Intents: direct messages + guild messages + message content.

A message is accepted only if it is not from a bot (loop guard, covers the
bot's own messages), the sender id equals `config.DiscordUserID` (**the
allowlist is required** — empty rejects everyone, since anyone can share a
guild with the bot), and it is either a **DM** or a message in
`config.DiscordGuildID` that **@mentions the bot** (mirrors common openclaw
plugin behavior: guild requires @mention, DM does not — the mention itself is
stripped from the turn text). Everything else is skipped at debug level.

Each accepted message waits for the agent to go idle (`IsBusy`, 500 ms poll,
ctx-aware), then is injected via `sendChat` with flow source `discord` and
the sender-metadata prefix
`[discord] Message from <Username> [id:<id>]:\n<text>`. The run is marked
**silent** (no TTS) and tracked in `discordRuns` (runID → channel id); while
the turn runs, a `discordTypingKeeper` goroutine keeps Discord's **native
typing indicator** alive (`ChannelTyping` immediately and every 8 s — the
indicator lasts ~10 s — capped at 10 minutes). At `turn.completed`,
`emitFinal` consumes the tracker and posts the reply —
`stripForChannel`-cleaned, like telegram — back to the channel via
`ChannelMessageSend`, **chunked at Discord's hard 2000-character message
limit** (splitting on newline boundaries when possible); on `turn.failed` the
tracker is consumed without a reply so the map cannot leak. The reply sender
uses a mutex-guarded session handle on the service (nil session → log +
drop).

Requirements (config.json): `discord_bot_token`, `discord_user_id`
(allowlist), and `discord_guild_id` when guild mentions should work (DM-only
setups can leave it empty).

**Managed Discord (shared-bot, `discord_managed`).** `CodexService` implements
`domain.DiscordBridge`, so Discord can also run with **no on-device token and no
session** — the bff-campaign-service relay owns the shared bot. When
`discord_managed`, `startDiscordBot` skips opening the discordgo session; inbound
messages arrive over MQTT (`discord_event` → `HandleInboundDiscord`, the relay
supplying `bot_user_id` / `mentions_bot`). The reply stays **internal** — `emitFinal`
posts it, and `finishDiscordTurn` / `sendDiscordTyping` take a managed branch that
routes to the `ChannelRelay` (`discord_reply` / `discord_typing` on fd_channel)
instead of `ChannelMessageSend`. Codex does **not** implement `DiscordReplyDeliverer`
(that is for os-server-finalized runtimes like hermes/openclaw), so os-server never
double-delivers. See [`adding-agent-runtime.md`](adding-agent-runtime.md) §9 and
[`mqtt.md`](../mqtt.md).

### Channel API

`SupportedChannels()` returns `["telegram", "slack", "discord"]`.
`AddChannel` / `RefreshChannelConfig` for all three are honest no-op
successes — every consumer reads the creds fresh from config.json on each use
(the telegram loop per iteration, the Slack bridge per event / Web API call,
the discord bot per connect attempt / message), so there is nothing
agent-side to write and no restart needed. `AddChannel(discord)` additionally
validates that `discord_bot_token` and `discord_user_id` are present (the
receive path cannot work without them — accepting would be fake success).
Whatsapp has no receive path and returns `domain.ErrChannelNotSupported`. See
also [`adding-agent-runtime.md`](adding-agent-runtime.md).

### Telegram remote coding-sessions (`telegram_coding.go`, `coding_sessions.go`)

Mirrors `runtimes/claudecode/telegram_coding.go` 1:1 — a Telegram chat can
**attach to a folder's interactive `codex` thread and continue coding it from
the phone**, across multiple folders each with its own thread. Separate from the
device-main persona turn.

- **Discovery** (`coding_sessions.go`): codex stores each thread as a "rollout"
  JSONL under `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`
  (`/root/.codex/sessions`). `allCodingSessions` walks that tree; each rollout's
  **thread id (`payload.id`) and cwd (`payload.cwd`) come from its first
  `session_meta` record**; the listing shows the **3 most recent user prompts**
  (most-recent first, synthetic `<environment_context>` blocks skipped). Entries
  are deduped by thread id (newest rollout wins). Unlike claude, codex resume is
  **thread-id-global**: `codex exec --cd <dir> resume <id>` sets the cwd
  independently and does not require it to match the original (device-verified —
  resuming an old thread echoes its id back; a 404 in that test was the separate
  campaign-api `/responses` endpoint gap, not the resume mechanism).
- **Commands** (intercepted in `handleTelegramUpdate` before the device-main
  injection): `/resume` (mirrors the codex CLI — no arg lists folders, `/resume
  <n>` picks by number, `/resume <folder>` picks the newest) · aliases
  `/sessions` (list) + `/use <n|folder>` (pick) · `/sessions <folder>` (all
  threads in one folder) · `/new <folder>` · `/here` · `/device` · `/help`. A
  chat with no selection and no command falls through to device-main unchanged.
- **Hand-off model.** Each accepted turn spawns a fresh `codex exec --json
  --dangerously-bypass-approvals-and-sandbox --cd <folder> [resume <thread>]
  <prompt>` (flag order matters — `--cd` is rejected after `resume`, so it rides
  before). The reply is the accumulated `agent_message` items parsed from the
  JSONL (`parseCodexResult`: `thread.started` → id, `item.completed`/
  `agent_message` → text, `turn.completed` → done), DMed chunked at Telegram's
  4000-char limit. The exec env **mirrors the gatewayd's `turnEnv`** (process env
  + presync `.env` pairs + `HOME=/root` + `CODEX_HOME=/root/.codex`), so codex
  resolves the same `config.toml` + auth — working in **both** auth modes
  (`OPENAI_API_KEY` via `/responses`, or the ChatGPT-subscription `auth.json`)
  with no runner change.
- **Guards.** A per-folder mutex serializes turns; a `/proc` scan
  (`procHoldsFolder`) refuses a turn while an interactive `codex` TUI still holds
  the folder (two writers would corrupt the rollout). Only the allowlisted
  `telegram_user_id` reaches any of this; remote coding runs
  `--dangerously-bypass-approvals-and-sandbox`, so the allowlist is the security
  boundary.
- **State.** Per-chat selection persists to `/root/.codex/telegram_coding.json`
  (survives restart). Runs in os-server directly (its own `codex exec` per turn),
  independent of the persistent gatewayd child.

## 6. Hooks

Codex ships no hooks loader, so OpenClaw's `emotion-acknowledge` hook is
reproduced **natively in Go** (`runtimes/codex/emotion_ack.go`, mirroring
hermes' `emotion_ack.go`): on each user-visible turn, sendChat fires
`{emotion:"thinking"}` to HAL — same skip prefixes, same intensity, same
capability gate (`skills.SupportedHooks`) as the TS handler. The companion
`turn-gate` hook is intentionally not mirrored (sendChat already marks the turn
busy). ⚠️ Keep it in lockstep with `runtimes/openclaw/hooks/emotion-acknowledge/handler.ts`.

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

### Subscription auth (manual)

Available today without the phase-2 pairing flow: run
`codex login --device-auth` on the device, or copy an existing
`~/.codex/auth.json` from another machine to `/root/.codex/auth.json`
(`chmod 600`). Presync auto-detects `auth.json` on every run (so on every
boot): it omits the custom provider block from config.toml and drops
`OPENAI_API_KEY` from `.env`, so codex talks to OpenAI directly with its
built-in default provider + model — this **bypasses the campaign-api
`/responses` 404 blocker** entirely. Delete `auth.json` to fall back to
api-key mode; the flip is automatic on the next presync run.
