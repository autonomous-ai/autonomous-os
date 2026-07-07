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
> Discord via Claude Code's **native channel plugins**
> ([code.claude.com/docs/en/channels](https://code.claude.com/docs/en/channels)),
> and the **claude.ai OAuth login** flow (§7b) as an alternative to the
> config.json API key. Known caveats are flagged ⚠️ in §11.

Code: `os/services/internal/claudecode/`.

| What | Where on device |
|------|-----------------|
| Claude Code CLI | `/usr/local/bin/claude` (symlink → `/root/.local/bin/claude`) |
| Bridge (systemd `claudecode.service`) | `/root/.claudecode/bridge.py` (presync-materialized) |
| Launch env (`ANTHROPIC_*`, channel flags) | `/root/.claudecode/.env` (presync-owned) |
| Workspace (Claude's cwd) | `/root/.claudecode/workspace/` |
| Persona / memory | `workspace/{CLAUDE,SOUL,IDENTITY,USER,MEMORY,KNOWLEDGE}.md`, `workspace/memory/*.md` |
| Skills | `workspace/.claude/skills/<name>/` (native Claude Code skills) |
| MCP connectors | `workspace/.mcp.json` |
| Session-resume state | `/root/.claudecode/session.json` |
| Channel config (telegram / discord) | `/root/.claude/channels/<ch>/{.env,access.json}` |
| claude.ai OAuth credentials (login flow) | `config.json` `claude_code_oauth_token` + `/root/.claude/.credentials.json` |
| Conversation transcripts | `/root/.claude/projects/` (Claude-internal) |

---

## 1. Selection + install

`config.agent_runtime: "claudecode"` (or DEVICE.md `gateway.default`) resolves the
backend in `internal/agent/factory.go`. Switching in/out goes through the generic
`switch-runtime` flow — nothing claudecode-specific in the switcher.

**`install.sh`** (embedded, runs once on first switch / failed verify):

1. prerequisites: `jq curl git python3` + the python `websockets` library
   (`python3-websockets`, pip fallback with `--break-system-packages`);
2. Claude Code CLI via the official native installer
   (`curl -fsSL https://claude.ai/install.sh | bash` → `~/.local/bin/claude`,
   standalone binary, linux arm64/amd64, no Node.js), symlinked to
   `/usr/local/bin/claude`;
3. **bun** + the **telegram + discord channel plugins** (best-effort):
   `claude plugin marketplace add anthropics/claude-plugins-official` +
   `claude plugin install {telegram,discord}@claude-plugins-official`. Channel
   plugins are bun scripts; a failure here only disables the channel receive
   loops (⚠️ §11);
4. runs the presync hook once (bridge + env + channel sync);
5. writes + starts **`claudecode.service`** (unit name == runtime name — no
   service declaration file). The unit runs `python3 /root/.claudecode/bridge.py`.
   The unit body is duplicated in `gateway_unit.go` (`EnsureOnboarding`
   self-heal) — **keep the two in sync**;
6. verify hook `/usr/local/lib/os-runtimes/claudecode/verify` =
   `command -v claude` (cheap, CLI-only — presync heals everything else).

**`presync.sh`** (embedded; materialized to
`/usr/local/bin/runtime-claudecode-presync` on every switch, run by
switch-runtime before start, by install.sh once, and by `EnsureOnboarding` on
**every os-server boot / config change** — the hermes pattern, so a device that
boots straight into claudecode or edits `llm_*`/telegram while active self-heals
without a switch):

- **§0 BRIDGE** — heredoc-writes `bridge.py` (always overwritten → a plain
  os-server OTA refreshes the bridge; nothing reset-fragile lives in install.sh);
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
  - *api-key* (default): `ANTHROPIC_BASE_URL` ← `llm_base_url` (default
    `https://campaign-api.autonomous.ai/api/v1/ai`, **no trailing `/v1`** —
    Claude calls `{base}/v1/messages`, the same anthropic-messages endpoint
    hermes uses), `ANTHROPIC_API_KEY` **and** `ANTHROPIC_AUTH_TOKEN` ←
    `llm_api_key` (x-api-key and Bearer conventions both covered),
    `ANTHROPIC_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` ← `llm_model` (default
    `Auto-AI`).

  Both modes add `DISABLE_AUTOUPDATER=1`,
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, and the `CLAUDECODE_CHANNELS`
  launch flags;
- **§3 CHANNELS** — see §7.

## 2. Wire constants (`constants.go`)

| Constant | Value | Meaning |
|---|---|---|
| `WSURL` | `ws://127.0.0.1:18791/claude/ws/` | bridge WebSocket endpoint |
| `Token` | `autonomous_claudecode_token` | bearer token on connect; baked into bridge.py by presync — the two MUST match |
| `Conversation` | `device-main` | label only; Claude owns the real session ids |

## 3. The bridge (`bridge.py`)

Claude Code has no server mode, so the systemd unit runs a ~270-line asyncio
bridge that:

- holds **one persistent headless Claude process**:
  `claude --print --verbose --input-format stream-json --output-format
  stream-json --dangerously-skip-permissions`, cwd = the workspace, env from
  `.env`; `--resume <session_id>` on respawn (session continuity across bridge
  restarts, state in `session.json`); `--channels <CLAUDECODE_CHANNELS>` when
  presync configured a channel plugin;
- serves the WebSocket (`websockets` lib, v10/v12 API-tolerant), bearer-token
  gated;
- forwards Claude's stdout stream-json events **verbatim** to all connected
  clients, converts `message.send` frames into stream-json `user` messages on
  stdin (data-URL image attachments → base64 `image` content blocks), answers
  `ping` with `pong`;
- restarts the child on exit (5 s backoff); when a turn was in flight it emits
  `bridge.error` so os-server closes the run instead of waiting out the busy
  TTL; `session.new` restarts the child **without** `--resume`;
- queues `message.send` frames that arrive while the child is down and flushes
  them on respawn.

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
| `result` subtype `success` | `chat.final` + `lifecycle.end` (+ per-turn `usage`) |
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
- **Channel-initiated turns surface on the same stdout**: a Telegram message
  handled by the plugin inside the Claude session produces assistant/result
  events with no pending runID — `ensureTurnStarted` allocates a fresh one, so
  those turns show up in the Flow Monitor (no observer hook needed, unlike
  hermes).

## 6. Session

Claude owns the session: the id is captured from any event carrying
`session_id` and persisted by the bridge (`session.json`) for `--resume`.
`NewSession` sends `{"type":"session.new"}` (fresh session, no resume).
`ShouldRotateSession` is **always false** and `CompactSession` returns
`domain.ErrNotSupportedByRuntime` — Claude Code auto-compacts its own context,
so an os-server-driven rotation would only throw context away.

## 7. Channels — Telegram + Discord via the native channel plugins

`SupportedChannels() = [telegram, discord]`. Unlike hermes/picoclaw
(device-owned receive loop), the loops here are **Claude Code's own channel
plugins**: the bridge launches `claude --channels
plugin:telegram@claude-plugins-official plugin:discord@claude-plugins-official`
(only the configured ones), each plugin polls its Bot API and replies through
the same chat, entirely inside the Claude session.

- presync writes `~/.claude/channels/<ch>/.env` (`TELEGRAM_BOT_TOKEN` ←
  `telegram_bot_token`, `DISCORD_BOT_TOKEN` ← `discord_bot_token`) and seeds
  `access.json` with `{"dmPolicy":"allowlist","allowFrom":["<owner user id>"]}`
  (`telegram_user_id` / `discord_user_id` — a Discord *snowflake*) — replacing
  the interactive `/telegram:access pair` / `/discord:access pair` flows, which
  a headless device cannot run. Both plugins share the same access.json schema.
  **The owner user id is required** for inbound messages; with only a token the
  plugin stays in pairing mode and drops strangers.
- `AddChannel`/`RefreshChannelConfig` (telegram/discord) → re-run presync +
  hash-diff restart (`syncChannels` → `EnsureOnboarding`, the hermes pattern).
  Other channels → `domain.ErrChannelNotSupported`.
- **No slack**: Claude Code has no slack channel plugin — "Claude in Slack" is
  a separate cloud feature that spawns web sessions from `@Claude` mentions,
  not a device channel. Slack creds in config.json are surfaced as
  `unsupported_channels` by `ChannelReconcile` and restored when switching back
  to a runtime that can run them.
- Outbound `Broadcast`/`SendToUser` (proactive nudges) go straight to the
  Telegram Bot API (`telegram_sender.go`). The shared target store
  (`/root/.lumi/telegram_targets.json`) is not populated by the plugin, so
  `GetTelegramTargets` falls back to the configured owner id (`telegram.go`).

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
software): the claude CLI, bun, `~/.claude/plugins`, `~/.claude.json`,
`bridge.py`. Everything wiped has a restore path that runs after the reset
(presync/EnsureOnboarding rebuild env + channels from the re-entered
config.json; `ensureSkills` re-downloads skills; the login flow re-runs for
subscription auth).

## 11. ⚠️ Verify on device

- **Channels are a research preview** (Claude Code ≥ 2.1.80): the `--channels`
  flag/protocol may change, and an org-managed account needs `channelsEnabled`.
  If a plugin fails to register, everything except that channel's receive loop
  still works.
- **`claude setup-token` output format**: the login scanner matches the OAuth
  URL, the `sk-ant-oat01-…` token, and textual success markers — verify against
  the CLI build on the device (the flow degrades to
  `failure (last output: …)` with the CLI's final line when parsing misses).
  The pty writes use `\r` for Enter; confirm the code-paste prompt accepts it.
- **`claude plugin` CLI availability**: install.sh treats marketplace/plugin
  install as best-effort; verify the CLI build on the device supports headless
  plugin install, or install the plugin once manually.
- **campaign-api compatibility**: Claude Code drives the full Anthropic
  Messages API (system prompts, tool_use loops, streaming) against
  `ANTHROPIC_BASE_URL` — verify the proxy passes these through (hermes already
  uses the same endpoint in `anthropic_messages` mode).
- **Headless first-run**: presync seeds `~/.claude.json` flags; if the CLI adds
  new interactive gates, the bridge child may exit on start — check
  `journalctl -u claudecode` and `claude` stderr lines in the bridge log.
