# Claude Code Agent Backend

How `os-server` drives **Claude Code** (Anthropic's CLI agent) as the device's
swappable agentic runtime, alongside OpenClaw, Hermes, and PicoClaw. Generic
mechanics (switch flow, install-vs-presync, migration, checklist) live in
[`adding-agent-runtime.md`](adding-agent-runtime.md); this file is the
claudecode-specific protocol, layout, and quirks.

> **Status:** full parity with the checklist — embedded install + presync,
> WebSocket bridge transport, persona/memory Go migration adapter (lossless with
> the OpenClaw layout), skills (CDN restore + watcher, native `.claude/skills`),
> identity watch/rename, real MCP (`.mcp.json`), factory reset, Telegram +
> Discord device-owned (os-server getUpdates loop `telegram_poll.go` /
> discordgo session `discord.go` — the native channel plugins are deliberately
> not used),
> device-owned **Slack inbound** (HTTP mode, `domain.SlackBridge`),
> and the **claude.ai OAuth login** flow (§7b) as an alternative to the
> config.json API key. Known caveats are flagged ⚠️ in §11.

Code: `os/services/internal/claudecode/`.

| What | Where on device |
|------|-----------------|
| Claude Code CLI | `/usr/local/bin/claude` (symlink → `/root/.local/bin/claude`) |
| Bridge (systemd `claudecode.service`) | `os-server claudecode-gatewayd` subcommand (compiled into `/usr/local/bin/os-server`; code `os/services/internal/claudecode/gatewayd/`) |
| Launch env (`ANTHROPIC_*`, channel flags) | `/root/.claudecode/.env` (presync-owned) |
| Workspace (Claude's cwd) | `/root/.claudecode/workspace/` |
| Persona / memory | `workspace/{CLAUDE,SOUL,IDENTITY,USER,MEMORY,KNOWLEDGE}.md`, `workspace/memory/*.md` |
| Skills | `workspace/.claude/skills/<name>/` (native Claude Code skills) |
| MCP connectors | `workspace/.mcp.json` |
| Session-resume state | `/root/.claudecode/session.json` |
| Telegram poll offset (device-owned loop) | `/root/.claudecode/telegram_offset.json` |
| claude.ai OAuth credentials (login flow) | `config.json` `claude_code_oauth_token` + `/root/.claude/.credentials.json` |
| Conversation transcripts | `/root/.claude/projects/` (Claude-internal) |

---

## 1. Selection + install

`config.agent_runtime: "claudecode"` (or DEVICE.md `gateway.default`) resolves the
backend in `internal/agent/factory.go`. Switching in/out goes through the generic
`switch-runtime` flow — nothing claudecode-specific in the switcher.

**`install.sh`** (embedded, runs once on first switch / failed verify):

1. prerequisites: `jq curl`;
2. Claude Code CLI via the official native installer
   (`curl -fsSL https://claude.ai/install.sh | bash` → `~/.local/bin/claude`,
   standalone binary, linux arm64/amd64, no Node.js), symlinked to
   `/usr/local/bin/claude`;
3. **no bun, no channel plugins** — telegram + discord are device-owned
   (os-server runs the receive loops itself, §7), so the plugin marketplace
   step is gone entirely;
4. runs the presync hook once (bridge + env + channel sync);
5. writes + starts **`claudecode.service`** (unit name == runtime name — no
   service declaration file). The unit runs `os-server claudecode-gatewayd`
   (with `EnvironmentFile=-/root/.claudecode/.env`). The unit body is duplicated
   in `gateway_unit.go` (`EnsureOnboarding` self-heal) — **keep the two in
   sync**;
6. verify hook `/usr/local/lib/os-runtimes/claudecode/verify` =
   `command -v claude` + an executable `/usr/local/bin/os-server` (the gatewayd
   ships inside it; presync heals everything else).

**`presync.sh`** (embedded; materialized to
`/usr/local/bin/runtime-claudecode-presync` on every switch, run by
switch-runtime before start, by install.sh once, and by `EnsureOnboarding` on
**every os-server boot / config change** — the hermes pattern, so a device that
boots straight into claudecode or edits `llm_*` while active self-heals
without a switch):

- the bridge itself is **no longer materialized here** — it ships inside the
  os-server binary as the `claudecode-gatewayd` subcommand, so a plain
  os-server OTA updates it;
- **§1 SEEDS** — `~/.claude.json` gets `hasCompletedOnboarding` +
  `bypassPermissionsModeAccepted` (no TTY to answer interactive prompts);
  `workspace/.claude/settings.json` gets `enableAllProjectMcpServers: true`
  (trust `.mcp.json` entries written by os-server);
- **§2 ENV** — `/root/.claudecode/.env`, in one of two **auth modes** (§7b):
    - *subscription* (`claude_code_oauth_token` set in config.json, or
      `~/.claude/.credentials.json` on disk): inject `CLAUDE_CODE_OAUTH_TOKEN` and
      **omit every `ANTHROPIC_*` var** — API-key vars outrank OAuth in Claude
      Code's credential precedence, so leaving them set would silently keep the
      device on the API-key path;
    - *api-key* (default): `ANTHROPIC_BASE_URL` ← `llm_base_url` **with a
      trailing `/v1` stripped** (llm_base_url is OpenAI-convention and ends in
      `/v1`; Claude appends `/v1/messages` itself, so an unstripped base hits
      `/v1/v1/messages` → 404), `ANTHROPIC_API_KEY` ← `llm_api_key`
      (**x-api-key only** — campaign-api 401s the `Authorization: Bearer` form,
      and claude prefers `ANTHROPIC_AUTH_TOKEN` over `ANTHROPIC_API_KEY` when
      both are set, so the bearer var must stay unset), `ANTHROPIC_MODEL` /
      `ANTHROPIC_SMALL_FAST_MODEL` ← `llm_model` (default `Auto-AI`).

  Both modes add `DISABLE_AUTOUPDATER=1` and
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`. `CLAUDECODE_CHANNELS` is no
  longer written (no channel plugins run — §7);
- **§3 CHANNELS** — nothing to configure anymore: presync just removes stale
  `~/.claude/channels` state from older versions (see §7).

## 2. Wire constants (`constants.go`)

| Constant | Value | Meaning |
|---|---|---|
| `WSURL` | `ws://127.0.0.1:18791/claude/ws/` | bridge WebSocket endpoint |
| `Token` | `autonomous_claudecode_token` | bearer token on connect; the gatewayd defaults to the same constant (compiled into os-server) — the two MUST match |
| `Conversation` | `device-main` | label only; Claude owns the real session ids |

## 3. The bridge (`os-server claudecode-gatewayd`)

Claude Code has no server mode, so the systemd unit runs a small Go gatewayd
(`internal/claudecode/gatewayd/`, structurally mirroring the codex gatewayd —
no python3/websockets dependency) that:

- holds **one persistent headless Claude process**:
  `claude --print --verbose --input-format stream-json --output-format
  stream-json --dangerously-skip-permissions`, cwd = the workspace, env from
  `.env` plus asserted `HOME` and `IS_SANDBOX=1` (the device runs as root, and
  claude refuses `--dangerously-skip-permissions` under uid 0 without the
  containerized-root escape hatch); `--resume <session_id>` on respawn
  (session continuity across bridge restarts, state in `session.json`). The
  gatewayd still supports a `--channels <CLAUDECODE_CHANNELS>` passthrough,
  but presync no longer sets the var (channels are device-owned, §7);
- serves the WebSocket (gorilla/websocket), bearer-token gated (close code
  `4401` on a bad token); a single client at a time — a new connection replaces
  the old one;
- forwards Claude's stdout stream-json events **verbatim** to the connected
  client, converts `message.send` frames into stream-json `user` messages on
  stdin (data-URL image attachments → base64 `image` content blocks), answers
  `ping` with `pong`;
- restarts the child on exit (5 s backoff); when a turn was in flight it emits
  `bridge.error` so os-server closes the run instead of waiting out the busy
  TTL; `session.new` restarts the child **without** `--resume`;
- queues `message.send` frames that arrive while the child is down and flushes
  them on respawn.

Paths, port and token are overridable via `CLAUDECODE_*` env vars
(`CLAUDECODE_WS_TOKEN`, `CLAUDECODE_PORT`, `CLAUDECODE_HOME`,
`CLAUDECODE_WORKSPACE`, `CLAUDECODE_ENV_FILE`, `CLAUDECODE_SESSION_FILE`,
`CLAUDECODE_BIN`, `CLAUDECODE_RESTART_BACKOFF_S`); the defaults match the
device layout above, so existing `/root/.claudecode` deployments run unchanged.

## 4. Sending a turn (`chat.go`)

Identical shape to picoclaw: `sendChat` marks busy + stashes the pending runID
**before** writing the frame, emits `chat_input`/`chat_send` flow events, and
returns as soon as the frame is written — the reply arrives on the read loop.
Outbound frame:

```json
{"type":"message.send","id":"chat-42","payload":{
  "content":"...","attachments":[{"type":"image","url":"data:image/jpeg;base64,..."}]}}
```

Claude serializes queued inputs itself, so one turn is in flight at a time and
the single pending/current runID correlation holds.

## 5. Inbound events → `domain.WSEvent` (`translator.go`)

Claude stream-json events are translated into the same frames the OpenClaw
handler consumes:

| Claude event | Emitted |
|---|---|
| `system` (subtype `init`) | — (captures `session_id`) |
| `assistant` — `text` block | — (stashed as fallback final text) |
| `assistant` — `tool_use` block | `lifecycle.start` (once) + `tool.start` |
| `user` — `tool_result` block | `tool.end` (result text, matched by `tool_use_id`) |
| `result` subtype `success` | assistant delta (whole reply, N=1) + `chat.final` + `lifecycle.end` (+ per-turn `usage`) — the delta is what the shared consumer accumulates and flushes to TTS/`tts_send` at `lifecycle.end` |
| `result` subtype `error*` / `is_error` | `lifecycle.error` |
| `bridge.error` | `lifecycle.error` |
| `stream_event` / `pong` / `bridge.status` | ignored |

Turn-lifecycle gotchas:

- **The final text is `result.result`**, not the assistant text blocks —
  intermediate assistant text (between tool calls) is never rendered, only
  stashed as a defensive fallback.
- `usage` is **per-turn** (Anthropic API shape: `input_tokens`,
  `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`) —
  unlike picoclaw's cumulative `context_usage`.
- **Externally-initiated turns surface on the same stdout**: if anything ever
  runs inside the Claude session without a pending runID (e.g. a channel
  plugin, were one enabled), `ensureTurnStarted` allocates a fresh runID so
  the turn still shows up in the Flow Monitor. With all channels device-owned
  (§7) this is a defensive path only — telegram/discord/slack turns inject as
  regular `sendChat` turns with a pending runID.

## 6. Session

Claude owns the session: the id is captured from any event carrying
`session_id` and persisted by the bridge (`session.json`) for `--resume`.
`NewSession` sends `{"type":"session.new"}` (fresh session, no resume).
`ShouldRotateSession` rotates on **turn count (80) or a 150k-token spike**
(`rotation.go`): Claude Code's auto-compaction bounds the context *size* but
not persona fidelity — after enough compaction cycles the one-line
IDENTITY.md name drifts out of the compacted context (device-observed
2026-07-08: the agent invented a name), and `CLAUDE.md` @imports are only
re-read at session start, so periodic rotation is the re-anchor. Long-term
memory (MEMORY.md/KNOWLEDGE.md) survives via the imports; only verbatim
in-session conversation is lost. `CompactSession` returns
`domain.ErrNotSupportedByRuntime` (no external compact RPC).

## 7. Channels — all device-owned (telegram, discord, slack)

`SupportedChannels() = [telegram, slack, discord]`. All three receive loops
run inside os-server, mirroring `internal/codex` 1:1. Claude Code's native
telegram/discord channel plugins are **deliberately not used**: they proved
undebuggable in the field (bun children with no journal logs, silent
allowlist drops, silent death on bridge-restart races), and they would compete
with the device-owned loops for the same bot (Telegram 409s concurrent
pollers; Discord would double-reply). presync removes stale
`~/.claude/channels` state and install.sh no longer installs bun or any
plugin.

- **Telegram** (`telegram_poll.go`): one goroutine started from `StartWS`
  long-polls `getUpdates` and injects each accepted DM (private chat, sender ==
  `telegram_user_id`) as a regular chat turn with flow source `telegram`; the
  run is tracked in `telegramRuns` and **marked silent** (no TTS), a typing
  keeper fires `sendChatAction` while the turn runs, and `emitFinal` DMs the
  reply back (`[HW:/...]` markers stripped via `stripForChannel`). Offset
  persists in `/root/.claudecode/telegram_offset.json`. Creds are read fresh
  from config.json every poll iteration — token/user changes need no restart.
- **Discord** (`discord.go`): Discord has no long-poll receive API, so
  os-server owns a **discordgo gateway session** (DM + guild-message +
  message-content intents), started from `StartWS`. Accepted = not a bot,
  sender == `discord_user_id` (empty allowlist = closed), and either a DM or a
  message in `discord_guild_id` that **@mentions the bot** (mention stripped
  from the turn text). Accepted messages inject as turns with flow source
  `discord` (tracked in `discordRuns`, silent, native typing keeper);
  `emitFinal` posts the reply back **chunked at Discord's 2000-char limit**.
  Token is read fresh on every (re)connect attempt; while a session is open,
  discordgo handles gateway reconnects itself.
- `AddChannel`/`RefreshChannelConfig` are honest no-op successes for all three
  channels: the device-owned loops read creds fresh from config.json on each
  use, so persisting the creds is all that is needed (no presync, no bridge
  restart). Discord nuance: a token saved the first time is picked up within
  ~30 s, but rotating it while a session is open takes effect on the next
  session cycle. whatsapp → `domain.ErrChannelNotSupported`.
- **Slack is DEVICE-OWNED** (`slack.go` + `slack_sender.go`, a mirror of
  `internal/codex/slack.go`): Claude Code has no slack channel plugin ("Claude
  in Slack" is a separate cloud feature that spawns web sessions from `@Claude`
  mentions, not a device channel). Instead the public bff-campaign-service
  proxy receives Slack Events API deliveries and fans them out over MQTT to the
  device's `slack_event` handler
  (`server/device/delivery/mqtt/slack_event_handler.go`), which dedups by
  event_id and type-asserts the active gateway to `domain.SlackBridge` —
  implemented by `ClaudeCodeService`, so events route here only while
  claudecode is active. An accepted message (allowlist gate `slack_user_id`;
  bot/subtype/empty-user events dropped) waits for the agent to go idle
  (500 ms poll, 2 min cap) and is injected as a regular turn with flow source
  `slack`; the run is tracked in `slackRuns` and **marked silent** (no TTS),
  and an `eyes` ack reaction is added to the user's message. The final answer
  (`result` event → `emitFinal`, `translator.go`) is posted back to the
  originating channel/thread via `chat.postMessage` (`slack_bot_token`),
  `[HW:/...]` markers + audio tags stripped (`stripForChannel`, `hal.go`); the
  ack reaction is cleared on reply or error. **No progressive streaming**
  (`StreamSlackDelta` is a no-op — the answer arrives whole). Replies thread
  under the existing thread, else under the user's message.
- Outbound `Broadcast`/`SendToUser` (proactive nudges) go straight to the
  Telegram Bot API (`telegram_sender.go`); a `SlackSender` posts proactive
  messages to the configured `slack_user_id` channel when both slack creds are
  set. The shared target store (`/root/.lumi/telegram_targets.json`) is
  populated by the receive loop (`upsertTelegramTarget`) on every accepted DM;
  `GetTelegramTargets` falls back to the configured owner id while the store
  is still empty (`telegram.go`).

## 7b. Auth — claude.ai OAuth login (alternative to the API key)

The device can authenticate with the **user's own Claude subscription** instead
of `llm_api_key`. The flow (`internal/claudecode/login.go`, the
`domain.ClaudeLoginPairer` optional interface) mirrors the WhatsApp pairing
flow — streaming `PairingEvent`s — with one extra leg: the OAuth code travels
back into the flow.

1. `{"cmd":"claudecode_login"}` on fa_channel starts the flow: os-server runs
   `claude setup-token` under a pty (`script -qec` — the CLI refuses to run
   without a TTY) and scans its output.
2. `{"status":"pairing_url","login_url":"https://claude.ai/oauth/…"}` is
   streamed on fd_channel; the user opens the URL, authorizes, and copies the
   code.
3. `{"cmd":"claudecode_login_code","code":"…"}` feeds the code to the waiting
   CLI (written to the pty with a raw-mode `\r`).
4. On success the long-lived token (`sk-ant-oat01-…`) is persisted to
   config.json (`claude_code_oauth_token`), `EnsureOnboarding` re-runs presync
   (subscription-mode `.env` — see §1), and the bridge restarts into
   subscription auth. Fallback: if the token line was not captured but the CLI
   saved `~/.claude/.credentials.json`, that also counts as success — presync
   accepts either signal.

Non-claudecode runtimes answer `claudecode_login` with a one-shot failure
("claude login not supported on … backend"). MQTT contract details:
`docs/mqtt.md` §`claudecode_login`. ⚠️ Credential precedence is the trap here:
`ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` **outrank** the OAuth token, which
is why subscription-mode presync omits them entirely.

## 7c. Telegram remote coding-sessions (`telegram_coding.go`, `coding_sessions.go`)

Separate from the device-main persona turn: a Telegram chat can **attach to a
folder's interactive `claude` session and continue coding it from the phone**.
Usecase — code on the device terminal at home, walk out, keep going over
Telegram, across multiple folders each with its own session.

- **Discovery** (`coding_sessions.go`): claude stores each session as a JSONL
  transcript under `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl` (root ⇒
  `/root/.claude/projects`). `allCodingSessions` walks that tree; each session's
  **real cwd is read from the transcript content** (a `cwd` field on its
  records), NOT decoded from the directory name — the `/`→`-` encoding is lossy
  for folders containing `-`. The listing shows the **3 most recent user
  prompts** (most-recent first) as the description — synthetic
  `<environment_context>`/`<system-reminder>` blocks the CLI injects as "user"
  messages are skipped (`isInjectedContext`). Sessions are **cwd-scoped**:
  `claude --resume <uuid>` only finds a session when run from its folder
  (device-proven: resuming from the wrong cwd returns `No conversation found`).
- **Commands** (intercepted in `handleTelegramUpdate` before the device-main
  injection): `/resume` (mirrors the claude CLI — no arg lists folders, `/resume
  <n>` picks by number, `/resume <folder>` picks the folder's newest) · aliases
  `/sessions` (list) + `/use <n|folder>` (pick) · `/sessions <folder>` (every
  session in one folder) · `/new <folder>` (fresh session, folder created if
  missing) · `/here` (current selection) · `/device` (back to the device-main
  persona) · `/help`. A chat with no selection and no command falls through to
  device-main unchanged.
- **Hand-off model, NOT co-editing.** Each accepted turn spawns a fresh `claude
  --print --output-format json [--resume <uuid>] --dangerously-skip-permissions`
  with `cmd.Dir` = the session folder and the prompt on stdin; the reply's
  `result`/`session_id` are parsed back (`parseClaudeJSONResult`) and the reply
  DMed (chunked at Telegram's 4000-char limit). The exec env = process env + the
  presync `.env` pairs (`ANTHROPIC_*`) + `IS_SANDBOX=1` + `HOME=/root` (root
  needs `IS_SANDBOX` for `--dangerously-skip-permissions`, same as the gatewayd
  child). A `/new` session's real uuid is captured from its first turn.
- **Guards.** A **per-folder mutex** serializes turns so two never append to one
  transcript. A **`/proc` scan** (`procHoldsFolder`) refuses a turn while an
  interactive `claude` TUI still holds the folder as its cwd — two writers would
  corrupt the transcript, so the model is hand-off (close the terminal session
  first). Only the allowlisted `telegram_user_id` reaches any of this (the same
  gate as ordinary Telegram turns); remote coding runs
  `--dangerously-skip-permissions`, so the allowlist is the security boundary.
- **State.** Per-chat selection persists to
  `/root/.claudecode/telegram_coding.json` (survives restart — the chat stays in
  its session). This runs in os-server directly (its own `claude` subprocess per
  turn), independent of the persistent gatewayd child, so coding turns and the
  device-main persona never collide.

## 8. Workspace, persona, skills, MCP

- **`CLAUDE.md`** is Claude Code's auto-loaded memory file; onboarding owns an
  OS-managed block (marker-delimited, owner notes preserved below) holding the
  persona **@imports** (`@SOUL.md @IDENTITY.md @USER.md @MEMORY.md
  @KNOWLEDGE.md` — CLAUDE.md is the only file Claude loads by name) and the
  prompt discipline (skills whitelist rule, memory rules, user priority),
  adapted from picoclaw's AGENTS.md block.
- **SOUL block**: same `soul_ref` device-soul injection as openclaw/picoclaw.
- **Persona migration** (`migrate_persona/runtime_claudecode.go`): the layout is
  deliberately **identical to OpenClaw's** (IDENTITY.md its own slot, MEMORY.md
  at the workspace root, KNOWLEDGE.md + daily `memory/*.md`), so every slot maps
  1:1 and round-trips with slot-bearing runtimes are structurally lossless.
  `CLAUDE.md` itself is runtime-specific and never carried.
- **No HEARTBEAT.md** — Claude Code has no heartbeat loop that would read it; a
  conscious skip, not an oversight.
- **Skills** live in `workspace/.claude/skills/` (native, auto-discovered).
  Capability-pruned on onboarding; **restored from the CDN when empty**
  (`ensureSkills` — covers factory reset); steady-state updates via
  `skill_watcher.go` (5-min OTA metadata poll, notify via
  `SendSystemChatMessage`).
- **MCP is real** (`mcp.go`): `WriteMCPEntry`/`RemoveMCPEntry` upsert
  `workspace/.mcp.json` `mcpServers` (canonical `{command,args,env}` /
  `{type,url,headers}` entries pass through verbatim) + bridge restart;
  `MCPReconcile` clones connectors on a runtime switch via
  `claudecode.ReadMCPEntries`.
- **LLM config migration** (`migrate_config/runtime_claudecode.go`): reads/writes
  `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL` in `/root/.claudecode/.env`.

## 9. What is real vs no-op (`stubs.go`)

Real (beyond the transport): `SetupAgent`/`EnsureOnboarding` (presync run +
workspace blocks + skills restore + unit self-heal, hash-diff restart),
`ResetAgent`, `RestartAgent`, `WatchIdentity`/`UpdateIdentityName`,
`StartSkillWatcher`, `WriteMCPEntry`/`RemoveMCPEntry`, `NewSession`,
`GetConfigJSON` (workspace settings.json — never the secret-bearing `.env`),
`Version` (`claude --version` probe), and the `domain.ClaudeLoginPairer`
surface (`StartClaudeLogin`/`SubmitClaudeLoginCode` — §7b).

No-op with reasons: `HasWhatsappSession`/`PairWhatsapp` (Baileys is
OpenClaw-only), `FetchChatHistory` (`TODO(claudecode-history)` — session JSONL
has no stable read API), `StartModelSync`/`StartPrimaryModelWatch` (fixed
model via env). `RefreshModelsConfig`/`UpdatePrimaryModel` (model =
`ANTHROPIC_MODEL`, presync-owned) and `CompactSession` (auto-compaction, §6)
return `domain.ErrNotSupportedByRuntime` so the caller falls back to
`EnsureOnboarding`, whose presync applies model changes;
`ShouldRotateSession=false`.

## 10. Factory reset (`reset.go`)

Stop + verify `claudecode.service`, disable it, then wipe **user data + creds**:
`workspace/`, `.env`, `session.json`, `~/.claude/projects`,
`~/.claude/channels`, `~/.claude/todos`, `~/.claude/history.jsonl`, and
`~/.claude/.credentials.json` (the claude.ai login — its config.json half,
`claude_code_oauth_token`, is wiped with the config). **Kept** (installed
software): the claude CLI, `~/.claude.json` (the bridge itself ships inside
the os-server binary). Everything wiped has a restore path that runs after
the reset (presync/EnsureOnboarding rebuild the env from the re-entered
config.json; the device-owned channel loops need nothing beyond config;
`ensureSkills` re-downloads skills; the login flow re-runs for subscription
auth).

## 11. ⚠️ Verify on device

- **`claude setup-token` output format**: the login scanner matches the OAuth
  URL, the `sk-ant-oat01-…` token, and textual success markers — verify against
  the CLI build on the device (the flow degrades to
  `failure (last output: …)` with the CLI's final line when parsing misses).
  The pty writes use `\r` for Enter; confirm the code-paste prompt accepts it.
- **campaign-api compatibility**: Claude Code drives the full Anthropic
  Messages API (system prompts, tool_use loops, streaming) against
  `ANTHROPIC_BASE_URL` — verify the proxy passes these through (hermes already
  uses the same endpoint in `anthropic_messages` mode).
- **Headless first-run**: presync seeds `~/.claude.json` flags; if the CLI adds
  new interactive gates, the bridge child may exit on start — check
  `journalctl -u claudecode` and `claude` stderr lines in the bridge log.
