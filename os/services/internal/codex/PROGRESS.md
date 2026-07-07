# Codex runtime — work log

Task: add OpenAI Codex CLI as the 4th agent runtime (openclaw / hermes / picoclaw / **codex**),
mirroring the `feature/claude-code` branch's blueprint (Claude Code runtime by another dev).
Branch: `feat/codex` (forked from `main` @ 118ff545, includes the ErrNotSupportedByRuntime
stub convention). This file is the running history for whoever picks the task up.

## Key design decisions (settled)

| Decision | Choice | Why |
|---|---|---|
| Transport | WS bridge on `ws://127.0.0.1:18792/codex/ws/`, token `autonomous_codex_token` | Mirror `feature/claude-code` (bridge.py + WS, picoclaw-shaped Go client) — team-established pattern |
| Bridge | Python (`bridge.py`, materialized by presync §0 heredoc, canonical source kept as `internal/codex/bridge.py`, spliced into the registered presync by `install.go` at Go init) | Same as claudecode branch; per-turn `codex exec --json` subprocess (codex exec is the stable automation surface — the official SDK wraps it; `codex app-server` is experimental/version-coupled) |
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
- [x] `bridge.py` (374 lines) — WS server, per-turn exec, session persistence, resume-retry,
      attachments via `-i`, verbatim event forwarding; py_compile + live fake-codex test PASS
- [x] `install.sh` — pinned binary download, prereqs, codex.service unit, verify hook; bash -n clean
- [x] `presync.sh` — §0 bridge heredoc (placeholder `#__BRIDGE_PY_SPLICED_AT_BUILD__`, spliced by
      install.go), §1 openclaw persona migrate (marker-gated), §2 config.toml (head-regen, preserve
      mcp_servers tail), §3 .env; bash -n clean
- [ ] Go package `internal/codex/` — adapt from `origin/feature/claude-code:os/services/internal/claudecode/`
      (sed-rename mechanical files; hand-rewrite: constants, translator, onboarding, mcp, reset,
      runtime, stubs, install)
- [ ] Glue: domain consts, factory case, version cache, logs.go, web AgentRuntimeSection
- [ ] Docs: docs/agentic/codex.md EN+VI, CLAUDE.md doc-table row
- [ ] go build / go test / tsc -b green
- [ ] Device verify (switch flow, first turn, resume, rotation, MCP write) — NOT started

## Gotchas discovered

- claudecode branch stubs predate the ErrNotSupportedByRuntime convention (returns bare nil) —
  do NOT copy its stubs verbatim; that branch also needs the fix when it merges.
- go-toml/v2 already in go.mod (indirect) — promote to direct for mcp.go.
- Bridge splice: `runtimereg.RegisterPresync` receives the presync content from `install.go`;
  splice `//go:embed bridge.py` into the placeholder there (keeps bridge.py lintable as a real file,
  no build script needed).
