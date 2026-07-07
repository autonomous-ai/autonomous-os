# Claude Code runtime — work log

Parity pass 2026-07-07 against the hardened codex runtime (`internal/codex/PROGRESS.md`).
This file tracks only what was verified/changed in that pass — it does not restate the
original `feature/claude-code` branch history.

## Progress

- [x] Stubs honesty: RefreshModelsConfig / UpdatePrimaryModel / CompactSession return
      `domain.ErrNotSupportedByRuntime` (never bare nil) — the EnsureOnboarding-presync
      fallback applies model changes from config.json, so callers still converge.
- [x] Glue wired: HAL enums / orchestrator / config plumbing for the claudecode runtime.
- [x] Info uplink: `claudecode_version` reported + logs source (journal mapping) added.
- [x] Persona determinism verified: `ensureClaudeMDBlock` re-asserts the OS block
      (with `@SOUL.md`/`@IDENTITY.md`/... imports) on every `EnsureOnboarding` run —
      exact-block fast path, stale-block strip, re-inject. `UpdateIdentityName` rewrites
      IDENTITY.md only; no CLAUDE.md refresh needed (@imports re-read at session start).
- [x] Channels honesty verified: telegram + discord native via Claude Code channel
      plugins (presync-owned token/allowlist sync, GetConfiguredChannel checks
      DiscordBotToken); whatsapp → `domain.ErrChannelNotSupported`; stale
      stubs.go comment (claimed discord unsupported) fixed.
- [x] Bridge ported to Go: bridge.py → `internal/claudecode/gatewayd` (`os-server
      claudecode-gatewayd` subcommand, codex-gatewayd file layout); presync no longer
      materializes bridge.py; install.sh drops the python3/websockets prereqs; unit
      ExecStart=/usr/local/bin/os-server claudecode-gatewayd (install.sh + gateway_unit.go).
- [x] Slack inbound (device-owned, HTTP mode): mirror of internal/codex/slack.go —
      `domain.SlackBridge` on ClaudeCodeService (slack.go + slack_sender.go), bff-proxy
      → MQTT slack_event path, allowlist `slack_user_id`, busy-wait inject (silent run,
      eyes ack), reply via chat.postMessage on the `result` event (emitFinal →
      finishSlackTurn, stripForChannel ported to hal.go); SlackSender proactive path;
      slack AddChannel/Refresh = honest no-op (creds read live); no streaming. Tests
      mirror codex slack_test.go. NOT device-verified.
