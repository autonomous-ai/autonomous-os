package claudecode

import (
	"context"
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"

	"go.autonomous.ai/os/domain"
)

// AddChannel + RefreshChannelConfig + SupportedChannels live in channels.go —
// Claude Code runs telegram natively via its channels plugin (the token/allowlist
// sync is presync-owned); slack/discord/whatsapp return domain.ErrChannelNotSupported.

func (s *ClaudeCodeService) HasWhatsappSession(_ string) bool { return false }

// PairWhatsapp — WhatsApp pairing requires a Baileys-style plugin which lives
// only in OpenClaw. Returns a one-shot failure event so the caller's drain loop
// exits cleanly.
func (s *ClaudeCodeService) PairWhatsapp(_ context.Context) <-chan domain.PairingEvent {
	ch := make(chan domain.PairingEvent, 1)
	ch <- domain.PairingEvent{
		Status: domain.PairingStatusFailure,
		Error:  "whatsapp pairing not supported on claudecode backend",
	}
	close(ch)
	return ch
}

// ResetAgent lives in reset.go — it stops the bridge, wipes the workspace +
// Claude Code session/channel state, and disables the unit (real work, not a stub).

// RestartAgent restarts the claudecode bridge via systemctl so callers that need
// a full reload (workspace / .env / .mcp.json re-read — all loaded at Claude
// session start) get it. Delegates to restartClaudeCodeGateway
// (service_gateway.go), which no-ops gracefully when systemctl is unavailable.
func (s *ClaudeCodeService) RestartAgent() error {
	slog.Info("RestartAgent: restarting claudecode bridge", "component", "claudecode")
	return restartClaudeCodeGateway()
}

// RefreshModelsConfig — the model is selected via ANTHROPIC_MODEL in
// /root/.claudecode/.env, which presync owns (synced from config.json llm_model).
// No-op here because EnsureOnboarding re-runs presync on every boot/config change.
func (s *ClaudeCodeService) RefreshModelsConfig() error {
	return nil
}

// EnsureOnboarding lives in onboarding.go — it re-runs the embedded presync
// (env + channel + bridge sync), keeps the OS-managed workspace blocks current,
// and restarts the bridge when anything changed.

// FetchChatHistory — Claude Code keeps its transcript as internal session JSONL
// under ~/.claude/projects; there is no stable read API for it.
// TODO(claudecode-history): translate the session JSONL into the monitor's
// history shape if the web chat view ever needs backfill. Returns empty so
// callers degrade gracefully.
func (s *ClaudeCodeService) FetchChatHistory(_ string, _ int) (json.RawMessage, error) {
	return nil, nil
}

// GetConfigJSON returns the workspace project settings
// (workspace/.claude/settings.json) — the only JSON config the backend owns that
// is safe to expose (no secrets; ANTHROPIC_* keys live in .env, never returned).
// Read-only; feeds the gw-config debug UI. Returns {} when absent.
func (s *ClaudeCodeService) GetConfigJSON() (json.RawMessage, error) {
	path := filepath.Join(claudecodeWorkspaceDir, ".claude", "settings.json")
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return json.RawMessage("{}"), nil
		}
		return nil, err
	}
	return json.RawMessage(data), nil
}

// WatchIdentity + UpdateIdentityName live in identity.go — Claude Code's
// IDENTITY.md is a 1-for-1 copy of OpenClaw's (imported into context via the
// CLAUDE.md @import line), so it watches/rewrites the `**Name:**` card line just
// like OpenClaw does.

// StartSkillWatcher lives in skill_watcher.go — it polls OTA metadata and
// auto-updates workspace/.claude/skills from the CDN (capability-gated).

// StartModelSync — the model registry is fixed (ANTHROPIC_MODEL env, presync-
// owned). No-op.
func (s *ClaudeCodeService) StartModelSync(ctx context.Context) {
	<-ctx.Done()
}

func (s *ClaudeCodeService) UpdatePrimaryModel(_ string) error {
	return nil
}

// StartPrimaryModelWatch — no openclaw.json-style agent config file to watch.
func (s *ClaudeCodeService) StartPrimaryModelWatch(ctx context.Context) {
	<-ctx.Done()
}

// GetConfiguredChannel — device config is the source of truth. Prefers
// telegram, then discord, then the generic label.
func (s *ClaudeCodeService) GetConfiguredChannel() string {
	if s.config.TelegramBotToken != "" {
		return "telegram"
	}
	if s.config.DiscordBotToken != "" {
		return "discord"
	}
	return "channel"
}

// CompactSession — no-op because Claude Code auto-compacts its own context when
// it approaches the window limit; there is no external compact RPC to call.
func (s *ClaudeCodeService) CompactSession(sessionKey string) error {
	slog.Info("CompactSession: no-op (claudecode auto-compacts)", "component", "claudecode", "session", sessionKey)
	return nil
}

// ShouldRotateSession — never rotate: Claude Code manages its own context window
// (auto-compaction), so an os-server-driven rotation would only throw context
// away. NewSession stays available for an explicit user reset.
func (s *ClaudeCodeService) ShouldRotateSession(_, _ int) bool {
	return false
}

// NewSession asks the bridge to restart Claude Code WITHOUT --resume, starting a
// fresh session; the local session id is dropped so the init event of the new
// session is adopted.
func (s *ClaudeCodeService) NewSession(sessionKey string) error {
	slog.Info("NewSession: requesting fresh claude session", "component", "claudecode", "key", sessionKey)
	s.sessionUUID.Store("")
	if err := s.sendFrame(map[string]any{"type": "session.new"}); err != nil {
		// Not connected — the bridge never saw the request, so its session.json
		// still resumes the old session on the next spawn. Best-effort by design:
		// rotation is user-triggered and can simply be retried once the bridge
		// is back.
		slog.Warn("NewSession: bridge not reachable", "component", "claudecode", "error", err)
	}
	return nil
}

// WriteMCPEntry + RemoveMCPEntry live in mcp.go — Claude Code natively reads
// workspace/.mcp.json, so MCP connector writes are real on this backend.
