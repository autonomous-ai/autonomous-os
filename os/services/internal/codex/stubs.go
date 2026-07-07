package codex

import (
	"context"
	"encoding/json"
	"log/slog"

	"go.autonomous.ai/os/domain"
)

// AddChannel + RefreshChannelConfig + SupportedChannels live in channels.go —
// telegram is device-owned (os-server runs the getUpdates receive loop, see
// telegram_poll.go); slack/discord/whatsapp return domain.ErrChannelNotSupported.

func (s *CodexService) HasWhatsappSession(_ string) bool { return false }

// PairWhatsapp — WhatsApp pairing requires a Baileys-style plugin which lives
// only in OpenClaw. Returns a one-shot failure event so the caller's drain loop
// exits cleanly.
func (s *CodexService) PairWhatsapp(_ context.Context) <-chan domain.PairingEvent {
	ch := make(chan domain.PairingEvent, 1)
	ch <- domain.PairingEvent{
		Status: domain.PairingStatusFailure,
		Error:  "whatsapp pairing not supported on codex backend",
	}
	close(ch)
	return ch
}

// ResetAgent lives in reset.go — Codex's factory-reset wipe (stop+disable
// gateway → rm -rf /root/.codex → recreate baseline dirs; presync re-asserts
// config.toml/.env on the next switch), so it does real work.

// RestartAgent restarts the codex gateway via systemctl so callers that need a
// full gateway reload (config/workspace re-read) get it. Delegates to
// restartCodexGateway (service_gateway.go), which no-ops gracefully when
// systemctl is unavailable (non-root / dev box).
func (s *CodexService) RestartAgent() error {
	slog.Info("RestartAgent: restarting codex gateway", "component", "codex")
	return restartCodexGateway()
}

// RefreshModelsConfig — Codex model config (config.toml model/base_url/env
// key) is owned by presync.sh. Returns ErrNotSupportedByRuntime so the caller
// falls back to EnsureOnboarding, whose presync re-reads llm_* from
// config.json and the hash gate restarts the gateway — i.e. the change is
// applied, just not by this method.
func (s *CodexService) RefreshModelsConfig() error {
	return domain.ErrNotSupportedByRuntime
}

// EnsureOnboarding lives in onboarding.go — it keeps the OS-managed block in the
// workspace AGENTS.md current (the rest of provisioning is owned by install.sh /
// presync.sh). Kept out of this stub file because it does real work.

// FetchChatHistory — Codex history is server-side and we don't walk it.
// Returns empty so callers degrade gracefully (also keeps the read loop's
// synchronous dispatch deadlock-free since the handler never blocks on a WS RPC).
func (s *CodexService) FetchChatHistory(_ string, _ int) (json.RawMessage, error) {
	return nil, nil
}

// GetConfigJSON — Codex's config is TOML (/root/.codex/config.toml), not
// JSON, and its secrets ride in .env; there is no JSON config file to expose.
// Returns ErrNotSupportedByRuntime so the gw-config UI reports "no
// device-side config file" instead of a misleading payload.
func (s *CodexService) GetConfigJSON() (json.RawMessage, error) {
	return nil, domain.ErrNotSupportedByRuntime
}

// WatchIdentity + UpdateIdentityName live in identity.go — Codex's IDENTITY.md
// is a 1-for-1 copy of OpenClaw's (same format), so it watches/rewrites the
// `**Name:**` card line just like OpenClaw does.

// StartSkillWatcher lives in skill_watcher.go — it polls OTA metadata and
// auto-updates workspace skills from the CDN (capability-gated), mirroring openclaw.

// StartModelSync — model registry is owned by Codex. No-op.
func (s *CodexService) StartModelSync(ctx context.Context) {
	<-ctx.Done()
}

// UpdatePrimaryModel — the Codex model is pinned in config.toml by presync
// (from config.json llm_model), not patched per-call. Same fallback contract
// as RefreshModelsConfig: sentinel → EnsureOnboarding presync applies it.
func (s *CodexService) UpdatePrimaryModel(_ string) error {
	return domain.ErrNotSupportedByRuntime
}

// StartPrimaryModelWatch — no agent-side config file to watch.
func (s *CodexService) StartPrimaryModelWatch(ctx context.Context) {
	<-ctx.Done()
}

// GetConfiguredChannel — Device config is the source of truth under Codex.
// Returns "telegram" when a bot token is set, otherwise the generic label.
func (s *CodexService) GetConfiguredChannel() string {
	if s.config.TelegramBotToken != "" {
		return "telegram"
	}
	return "channel"
}

// CompactSession — Codex does not expose a compact API; rotate via
// NewSession instead.
func (s *CodexService) CompactSession(sessionKey string) error {
	slog.Info("CompactSession: not supported (codex backend)", "component", "codex", "session", sessionKey)
	return domain.ErrNotSupportedByRuntime
}

// codexFallbackTokenThreshold is a safety net only: Codex auto-compacts its
// own context (model_auto_compact_token_limit), so the reported per-turn
// input stays bounded and this rarely fires. It exists so a runaway thread
// (compaction bug, oversized tool outputs) still gets rotated.
const codexFallbackTokenThreshold = 150_000

// ShouldRotateSession rotates on real reported token count (see
// domain.AgentGateway). turn.completed usage maps input+cached+output into
// TotalTokens (translator.go), which approximates the live context size.
func (s *CodexService) ShouldRotateSession(totalTokens, _ int) bool {
	return totalTokens > codexFallbackTokenThreshold
}

// NewSession tells the bridge to drop the persisted thread id (session.new
// frame) so the next `codex exec` starts a fresh thread, and clears the local
// session key. Best-effort when the socket is down: the local clear still
// happens and the bridge's stale thread id will fail resume → the bridge
// retries fresh on its own.
func (s *CodexService) NewSession(sessionKey string) error {
	slog.Info("NewSession: requesting fresh codex thread", "component", "codex", "key", sessionKey)
	s.sessionUUID.Store("")
	if err := s.sendFrame(map[string]any{"type": "session.new"}); err != nil {
		slog.Warn("session.new frame send failed (bridge will retry fresh on resume failure)",
			"component", "codex", "error", err)
	}
	return nil
}

// WriteMCPEntry + RemoveMCPEntry live in mcp.go — Codex keeps MCP servers in
// config.toml [mcp_servers.<name>], so they do real work.
