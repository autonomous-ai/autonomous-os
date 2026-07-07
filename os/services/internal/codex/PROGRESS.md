# Codex runtime — work log

Task: add OpenAI Codex CLI as the 4th agent runtime (openclaw / hermes / picoclaw / **codex**),
mirroring the `feature/claude-code` branch's blueprint (Claude Code runtime by another dev).
Branch: `feat/codex` (forked from `main` @ 118ff545, includes the ErrNotSupportedByRuntime
stub convention). This file is the running history for whoever picks the task up.

## Key design decisions (settled)

| Decision | Choice | Why |
|---|---|---|
| Transport | WS bridge on `ws://127.0.0.1:18792/codex/ws/`, token `autonomous_codex_token` | Mirror `feature/claude-code` (bridge.py + WS, picoclaw-shaped Go client) — team-established pattern |
| Bridge | **Go** (`internal/codex/gatewayd`, compiled into os-server; unit runs `os-server codex-gatewayd`) — user decision 2026-07-07 after weighing vs the claudecode branch's Python bridge: same-language repo, `go test` in CI, zero device deps (no python3/websockets), OTA rides os-server. Per-turn `codex exec --json` subprocess (exec is the stable automation surface; `codex app-server` is experimental/version-coupled). An earlier Python bridge.py existed briefly and was deleted. | |
| Session | `thread_id` from `thread.started`, persisted `/root/.codex/session.json`, `codex exec resume <id>`; `session.new` frame → fresh | History lives on disk under `$CODEX_HOME/sessions/` — process exit ≠ session loss |
| Auth (phase 1) | `OPENAI_API_KEY` = config.json `llm_api_key`, provider base_url = `llm_base_url` (campaign-api) | User decision: route through campaign-api. ChatGPT-subscription `codex login --device-auth` deferred to phase 2 (share pairing plumbing with claudecode's `ClaudeLoginPairer` after that branch merges) |
| Wire API | `wire_api = "responses"` — Codex removed chat-completions (~2/2026) | ⚠️ VERIFY ON DEVICE: campaign-api must serve `{base}/responses` |
| CLI install | Pinned GitHub release `rust-v0.142.5`, `codex-aarch64-unknown-linux-musl.tar.gz` → /usr/local/bin/codex | picoclaw's pinned-binary pattern; musl static, no runtime deps |
| Permissions | `--dangerously-bypass-approvals-and-sandbox` + config `approval_policy="never"`, `sandbox_mode="danger-full-access"` | Appliance running as root; must never block on approval (user requirement: must never block on approval prompts) |
| Instructions file | `AGENTS.md` in workspace — codex reads it natively; OS-managed block reused from picoclaw's (openclaw-derived) | Zero-translation persona slot |
| Channels | **Telegram device-owned inbound** (2026-07-07): os-server runs the getUpdates receive loop itself (`telegram_poll.go`), started from StartWS so it exists only while codex is the active runtime (no 409 poller conflicts — the hermes lesson). SupportedChannels=[telegram], AddChannel(telegram)=honest no-op success (creds in config.json drive the loop), slack/discord → ErrChannelNotSupported. UPDATE 2026-07-07: Slack inbound added via HTTP-mode proxy path (domain.SlackBridge in slack.go) → SupportedChannels=[telegram, slack]; discord/whatsapp still ErrChannelNotSupported. History: the original "telegram device-owned (picoclaw model)" copy was FALSE (picoclaw's inbound lives in its own binary) → corrected to none-inbound earlier on 2026-07-07 → real device-owned loop built the same day, closing TODO(codex-telegram) | Codex CLI has no channel layer, so the OS owns the receive loop |
| Stubs | Return `domain.ErrNotSupportedByRuntime` (never bare nil) | Main's new convention (docs/agentic/adding-agent-runtime.md §4 "No fake success") |
| MCP | `WriteMCPEntry` edits `/root/.codex/config.toml [mcp_servers.<name>]` via go-toml/v2; presync regenerates the config head but preserves the `[mcp_servers` tail | codex config.toml is the only MCP slot; supports streamable HTTP + headers |

## Research (verified against official docs 2026-07-07)

- `codex exec "<prompt>" --json` → JSONL: `thread.started` (thread_id), `item.started/completed`
  (item types: `agent_message`, `command_execution`, `mcp_tool_call`, `file_changes`, `web_search`),
  `turn.completed` (usage: input_tokens/cached_input_tokens/output_tokens), `turn.failed`.
- No streaming text deltas in exec mode — `agent_message` arrives whole → translator emits the
  picoclaw-style single-delta-then-final contract.
- `codex exec resume <id>`, sessions at `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl`.
- Images: `codex exec -i <path>` (repeatable).
- Auto-compaction built in (`model_auto_compact_token_limit`).
- Watch items: rollout JSONL grows unbounded (rotation policy on our side); pin CLI version per OTA;
  `codex proto` is gone, `app-server` experimental — do not build on them.

## Progress

- [x] Research + fact-check Codex CLI (agent report in session; summary above)
- [x] `feat/codex` branch from main; stale local claudecode files removed
- [x] **Bridge (Go)** `internal/codex/gatewayd/` — WS server (gorilla), auth 4401, single-client
      replace, per-turn `codex exec` (Setpgid group-kill, timeout), verbatim JSONL forwarding,
      session.json persistence, resume-retry-fresh, attachments via `-i`; 5 tests incl. `-race` PASS.
      Entry: `os-server codex-gatewayd` subcommand (cmd/os-server/main.go)
- [x] `install.sh` — pinned binary download (rust-v0.142.5 musl), jq/curl only, codex.service unit
      (`ExecStart=os-server codex-gatewayd`), verify hook; bash -n clean
- [x] `presync.sh` — §1 openclaw persona migrate (marker-gated, incl. AGENTS.md), §2 config.toml
      (head-regen, preserve [mcp_servers] tail, wire_api=responses), §3 .env; bash -n clean
- [x] Go package `internal/codex/` — full AgentGateway: translator.go (codex exec JSONL → WSEvent),
      onboarding (presync exec + hash-gated restart + AGENTS/SOUL/HEARTBEAT blocks), mcp.go
      (config.toml via go-toml/v2 + tests), stubs (ErrNotSupportedByRuntime convention;
      NewSession → session.new frame), reset (wipe /root/.codex), runtime (version probe),
      emotion_ack (hermes parity, wired in chat.go), channels: none inbound / outbound-only
      telegram sender (see Channels row above — the picoclaw copy was corrected 2026-07-07)
- [x] Glue: domain AgentRuntimeCodex + AgentRuntimes, factory case + transport map, version cache
      (handler_api_monitor + populate), logs.go journal:codex.service mapping, web
      AgentRuntimeSection blurbs (+picoclaw blurb backfilled)
- [x] Docs: docs/agentic/codex.md (243 lines) + docs/vi/agentic/codex_vi.md + CLAUDE.md table row
- [x] go build ./... + full go test + `tsc -b` + GOOS=linux GOARCH=arm64 build — ALL GREEN
- [x] Persona migration adapter `migrate_persona/runtime_codex.go` (OpenClaw-identical layout,
      rebrandToCodex + reCodex added to the other 3 adapters) → any-pair codex↔openclaw/hermes/
      picoclaw incl. reverse; presync §1 remains the skills carrier
- [x] LLM config migration adapter `migrate_config/runtime_codex.go` (read/write base_url in
      config.toml + OPENAI_API_KEY in .env; registered + CodexHome in DefaultOptions) —
      key/baseURL carries across switches both directions, before presync even runs
- [x] MQTT switch path: `codex.setup` kind (domain KindCodexSetup + dispatcher case →
      handleRuntimeSetup) — web AND MQTT can now switch
- [x] Deep verification vs REAL codex 0.142.5 binary + tag source (agent ran it):
      * FIXED CRITICAL: `--cd` after `resume` = "unexpected argument" → every resume silently
        fell back to fresh thread. Shared flags now go BEFORE the resume subcommand (turn.go).
      * FIXED: added "no rollout found" (verbatim 0.142.5 missing-thread error) to resumeErrHints.
      * CONFIRMED empirically: translator field names (item discriminator "type", usage names),
        generated config.toml passes --strict-config, mcp http_headers correct, release asset
        names + binary name, `codex --version` = "codex-cli 0.142.5", env_key=OPENAI_API_KEY
        alone authenticates (no login needed).
      * Known-flaky upstream: SIGKILL mid-turn can leave dangling rollout (issue #12382) —
        fresh-retry fallback covers it. Never add --ephemeral (kills resume).
- [x] Round-2 adversarial review (independent agent) + ALL 6 findings fixed:
      * HIGH: presync mcp_servers tail-grab broke after go-toml table reordering -> awk
        state machine, position-independent extraction; idempotency-tested twice
      * HIGH: telegram was dead (picoclaw /root/.lumi leftover; NO device-owned inbound
        loop exists — picoclaw's lives inside its own binary) -> codex is now HONEST:
        SupportedChannels=nil, ErrChannelNotSupported for all, outbound-only
        TelegramSender kept (explicit-ID DMs), TODO(codex-telegram)
      * MED: resume-attempt turn.failed held back until retry decision (gatewayd);
        queue-full -> bridge.status (not bridge.error); session.new rides the worker
        queue (ordering vs in-flight turn); gateway_unit.go wired into EnsureOnboarding
        (self-heal unit, content matches install.sh)
      * MED: translator no longer clobbers pendingRunID of a queued next turn
      * LOW: item.updated dedupe (ensureToolStart), install.sh exact-version guard,
        dead code removed (codexBin/envWithoutHome/picoStopVerifyTimeout), lying
        sed-artifact comments rewritten (onboarding header, IsBusy, identity, reset)
- [x] Behavioral audit of sed-adapted files: skills pipeline verified coherent —
      presync copy, skill_watcher CDN install, pruneUnsupportedSkills and the AGENTS.md
      hint ALL resolve to /root/.codex/workspace/skills (= codex exec --cd); markdown
      block changes no longer restart the gateway (codex reads workspace per-turn)
- [x] FINAL GATE: go build + full go test + go vet + tsc -b + GOOS=linux GOARCH=arm64
      — ALL GREEN (2026-07-07)
- [x] campaign-api /responses probe (2026-07-07, from lamp-ac82 with the device key):
      ALL variants 404 "Cannot POST" (/api/v1/ai/v1/responses, /api/v1/ai/responses,
      /api/v1/responses) → **BLOCKER CONFIRMED**: codex cannot chat through campaign-api
      until the backend adds an OpenAI Responses API passthrough. Interim options:
      point llm_base_url at api.openai.com (OpenAI key) to device-verify the pipeline,
      or wait for phase-2 ChatGPT-subscription auth (bypasses campaign-api entirely).
- [x] DEVICE-VERIFIED 2026-07-07 on lamp-ac82: codex CLI installed manually, subscription
      auth.json copied from operator Mac → **FIRST REAL TURN OK** (thread.started /
      agent_message / turn.completed, usage fields verbatim as the translator expects) and
      **RESUME OK** on the same thread with memory of the prior turn (validates the
      --cd-before-resume fix on hardware). campaign-api /responses still 404 (api-key mode
      remains blocked on backend).
- [x] presync subscription-mode gate: auth.json present → config.toml head WITHOUT
      model/model_provider/[model_providers.autonomous] (built-in provider) + OPENAI_API_KEY
      omitted from .env (key outranks/conflicts with ChatGPT auth); absent → api-key mode
      unchanged. Delete auth.json to fall back; presync re-runs every boot so the flip is
      automatic.
- [x] Device-owned Telegram inbound (`telegram_poll.go`, 2026-07-07): getUpdates long-poll
      (50s window; token + allowed user id re-read from config.json every iteration; only
      private-chat text from TelegramUserID accepted, rest skipped at debug with offset
      advanced), offset persisted atomically to /root/.codex/telegram_offset.json, accepted
      chat ids upserted into telegram_targets.json (Broadcast now has real targets), turns
      injected via sendChat with flow source "telegram" after an IsBusy wait, run marked
      silent + tracked in telegramRuns → emitFinal DMs the reply back with [HW:/...] markers
      and TTS audio tags stripped (stripForChannel, hal.go; mirrors handler_hw.go hwMarkerRe
      + HAL's audio-tag whitelist); handleError consumes the tracker (no leak). Channel API
      updated: SupportedChannels=[telegram], AddChannel(telegram) no-op success,
      RefreshChannelConfig(telegram)=("", nil). Hermetic httptest coverage in
      telegram_poll_test.go (offset persistence, allowlist rejects, single injection,
      run/silent marking). TODO(codex-telegram) closed. NOT device-verified.
- [x] Telegram inbound polish (2026-07-07): telegramTypingKeeper (sendChatAction typing
      immediately + every 4s until run consumed, capped at telegramTypingLifetime=10min) +
      sender-metadata prefix on injected turns via tgUser.label(). Device-verified 2026-07-07.
- [x] Slack inbound, HTTP mode (2026-07-07): CodexService implements domain.SlackBridge
      (slack.go, modeled on internal/hermes/slack.go), so the existing bff-proxy → MQTT
      slack_event dispatch (slack_event_handler.go type-assert at :151) routes events to
      codex with zero server-side diff. Parse/allowlist (config.SlackUserID)/thread
      fallback/eyes-ack mirror hermes; injection async (IsBusy poll 500ms, 2min cap),
      prefix "[slack] Message from <@U..> [channel:C..]:", run silent + tracked in
      slackRuns → emitFinal posts stripForChannel'd reply via chat.postMessage + clears
      the ack (slack_sender.go, config.SlackBotToken read per call); handleError consumes
      (no reply). SKIPPED vs hermes (intentional): progressive streaming (codex has no
      deltas — StreamSlackDelta no-op, post once on final) + assistant "…is typing" status;
      DeliverSlackReply = consume-if-present safety net (emitFinal consumes sync before
      dispatch → no double post). SlackSender registered for Broadcast (SlackUserID target).
      Channel API: SupportedChannels=[telegram, slack]; AddChannel/Refresh(slack) honest
      no-op (signing secret is consumed by the public proxy, not on device). Hermetic tests
      in slack_test.go (parse table, run-map round trip, inbound → injected turn + silent +
      eyes ack via slackSendTurn/slackAPIBase seams, emitFinal reply routing with marker
      strip, error cleanup). NOT device-verified.
- [x] Discord inbound (2026-07-07): device-owned Gateway WS bot session via
      github.com/bwmarrin/discordgo v0.29.0 (discord.go), started from StartWS like the
      telegram loop → active-runtime-only lifecycle, token read fresh per connect attempt
      (empty → 30s recheck, open error → 15s backoff), session closed on ctx.Done. Accept
      filter (pure func acceptDiscordMessage): non-bot author + sender == DiscordUserID
      (allowlist REQUIRED — empty rejects all) + DM, or guild == DiscordGuildID with
      bot @mention (mention stripped). Inject mirrors telegram: busy-wait 500ms, prefix
      "[discord] Message from <Username> [id:<id>]:", run silent + tracked in discordRuns
      → emitFinal posts stripForChannel'd reply via ChannelMessageSend chunked at 2000
      chars (newline-preferring chunker), handleError consumes (no reply, no leak); native
      typing keeper (ChannelTyping immediate + 8s, 10min cap). Session handle mutex-guarded
      on the service (nil → log + drop). Channel API: SupportedChannels=[telegram, slack,
      discord]; AddChannel(discord) validates DiscordBotToken+DiscordUserID, whatsapp stays
      ErrChannelNotSupported. Hermetic tests in discord_test.go (accept table, prefix,
      chunker boundaries/newline/runes, run-map round trip, inject + emitFinal routing +
      chunked reply + error cleanup via discordSendTurn/discordSendMessage seams). NOT
      device-verified.
- [ ] Device verify remainder (switch flow, rotation, MCP write) — first turn + resume done
      via subscription mode; api-key path still blocked on the /responses backend work above
- [ ] Device verify: persona inline block in AGENTS.md (pre-fix, codex introduced itself as "Codex" instead of the device persona name)
- [x] Device prepped for full switch test (2026-07-07): manual codex binary REMOVED
      (install.sh must install it), auth.json KEPT (root:root). ⚠️ USER DECISION:
      auth.json is TEST-ONLY — production mode is api-key via campaign-api once the
      backend ships /responses; at that point DELETE /root/.codex/auth.json and presync
      auto-flips back to api-key mode on the next boot.
- [ ] Phase 2: ChatGPT-subscription auth **pairing flow** (`codex login --device-auth` UX) —
      manual auth.json path works today (see gate above); deferred until the claudecode
      branch's login pairer merges (generalize domain.ClaudeLoginPairer, share MQTT flow)

## Gotchas discovered

- claudecode branch stubs predate the ErrNotSupportedByRuntime convention (returns bare nil) —
  do NOT copy its stubs verbatim; that branch also needs the fix when it merges.
- go-toml/v2 already in go.mod (indirect) — promote to direct for mcp.go.
- Bridge splice: `runtimereg.RegisterPresync` receives the presync content from `install.go`;
  splice `//go:embed bridge.py` into the placeholder there (keeps bridge.py lintable as a real file,
  no build script needed).
