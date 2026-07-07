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
| Auth (phase 1) | `OPENAI_API_KEY` = config.json `llm_api_key`, provider base_url = `llm_base_url` (campaign-api) | User decision: "qua campaign-api". ChatGPT-subscription `codex login --device-auth` deferred to phase 2 (share pairing plumbing with claudecode's `ClaudeLoginPairer` after that branch merges) |
| Wire API | `wire_api = "responses"` — Codex removed chat-completions (~2/2026) | ⚠️ VERIFY ON DEVICE: campaign-api must serve `{base}/responses` |
| CLI install | Pinned GitHub release `rust-v0.142.5`, `codex-aarch64-unknown-linux-musl.tar.gz` → /usr/local/bin/codex | picoclaw's pinned-binary pattern; musl static, no runtime deps |
| Permissions | `--dangerously-bypass-approvals-and-sandbox` + config `approval_policy="never"`, `sandbox_mode="danger-full-access"` | Appliance running as root; must never block on approval (user: "miễn là chạy không vấp") |
| Instructions file | `AGENTS.md` in workspace — codex reads it natively; OS-managed block reused from picoclaw's (openclaw-derived) | Zero-translation persona slot |
| Channels | Telegram only, device-owned (picoclaw model) | Codex has no channel delivery |
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
      emotion_ack (hermes parity, wired in chat.go), channels/telegram device-owned (picoclaw)
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
- [ ] Device verify (switch flow, first turn, resume, rotation, MCP write, campaign-api
      /responses endpoint) — NOT started
- [ ] Phase 2: ChatGPT-subscription auth (`codex login --device-auth`) — deferred until the
      claudecode branch's login pairer merges (generalize domain.ClaudeLoginPairer, share MQTT flow)

## Gotchas discovered

- claudecode branch stubs predate the ErrNotSupportedByRuntime convention (returns bare nil) —
  do NOT copy its stubs verbatim; that branch also needs the fix when it merges.
- go-toml/v2 already in go.mod (indirect) — promote to direct for mcp.go.
- Bridge splice: `runtimereg.RegisterPresync` receives the presync content from `install.go`;
  splice `//go:embed bridge.py` into the placeholder there (keeps bridge.py lintable as a real file,
  no build script needed).
