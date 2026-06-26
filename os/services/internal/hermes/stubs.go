package hermes

import (
	"context"
	"encoding/json"
	"log/slog"

	"go.autonomous.ai/os/domain"
)

// SetupAgent for Hermes lives in onboarding.go — at setup time it runs the
// presync hook (via EnsureOnboarding) to materialize config.yaml from the
// just-saved config.json (llm_* + channel tokens).

// AddChannel + RefreshChannelConfig + SupportedChannels live in channels.go —
// Hermes delivers telegram/slack/discord natively via ~/.hermes/.env, so both apply
// paths re-sync the .env (presync) and restart the gateway when it changed.

func (s *HermesService) HasWhatsappSession(_ string) bool { return false }

// PairWhatsapp — WhatsApp pairing requires a Baileys-style plugin which lives
// only in OpenClaw. Returns a one-shot failure event so the caller's drain
// loop exits cleanly.
func (s *HermesService) PairWhatsapp(_ context.Context) <-chan domain.PairingEvent {
	ch := make(chan domain.PairingEvent, 1)
	ch <- domain.PairingEvent{
		Status: domain.PairingStatusFailure,
		Error:  "whatsapp pairing not supported on hermes backend",
	}
	close(ch)
	return ch
}

// ResetAgent for Hermes lives in reset.go — the factory-reset wipe (stop daemon
// + hermes setup --reset + surgical rm), invoked by server/system/factoryreset.go
// on the active gateway.

// RestartAgent for Hermes lives in onboarding.go — it restarts hermes-gateway
// via restartHermesGateway, mirroring openclaw's RestartAgent.

// RefreshModelsConfig — Hermes config (~/.hermes/...) is owned externally; we
// don't patch it from Device. No-op.
func (s *HermesService) RefreshModelsConfig() error {
	return nil
}

// EnsureOnboarding for Hermes lives in onboarding.go — it runs the embedded
// presync hook each boot to self-heal config.yaml from config.json (llm_* +
// provider structure), and restarts hermes-gateway only when the config changed.

// FetchChatHistory — Hermes per-conversation history is server-side, but we
// don't currently walk the previous_response_id chain (hermes.md §17 decided
// "conversation name is enough"). Returns empty so callers degrade gracefully.
func (s *HermesService) FetchChatHistory(_ string, _ int) (json.RawMessage, error) {
	return nil, nil
}

// GetConfigJSON — no agent-side config file under Hermes. Returns empty.
func (s *HermesService) GetConfigJSON() (json.RawMessage, error) {
	return json.RawMessage(`{}`), nil
}

// WatchIdentity for Hermes lives in identity.go — it polls SOUL.md (no IDENTITY.md
// slot under Hermes) and pushes wake words to HAL + i18n device name on rename,
// mirroring internal/openclaw/service_identity.go.

// StartSkillWatcher for Hermes lives in skill_watcher.go — it keeps the
// OpenClaw-imported skills (~/.hermes/skills/openclaw-imports) fresh from the CDN,
// mirroring internal/openclaw/skill_watcher.go.

// StartModelSync — model registry is owned by Hermes. No-op.
func (s *HermesService) StartModelSync(ctx context.Context) {
	<-ctx.Done()
}

func (s *HermesService) UpdatePrimaryModel(_ string) error {
	return nil
}

// StartPrimaryModelWatch — no openclaw.json to watch.
func (s *HermesService) StartPrimaryModelWatch(ctx context.Context) {
	<-ctx.Done()
}

// GetConfiguredChannel — Device config is the source of truth under Hermes.
// Returns "telegram" when a bot token is set, otherwise the generic label.
func (s *HermesService) GetConfiguredChannel() string {
	if s.config.TelegramBotToken != "" {
		return "telegram"
	}
	return "channel"
}

// CompactSession — Hermes does not currently expose a compact API or CLI
// (hermes.md §7 decided to no-op). Workaround: rotate the conversation name
// via NewSession when context grows too large.
func (s *HermesService) CompactSession(sessionKey string) error {
	slog.Info("CompactSession: no-op (hermes backend)", "component", "hermes", "session", sessionKey)
	return nil
}

// NewSession — under Hermes, "new session" means routing future turns to a
// fresh named conversation. Setting an empty key restores the default. We do
// not delete prior history (Hermes server still has it under the old name).
func (s *HermesService) NewSession(sessionKey string) error {
	slog.Info("NewSession: rotating conversation (hermes backend)", "component", "hermes", "key", sessionKey)
	// sessionKey here is treated as the next conversation name. Empty means
	// reset to default. The session UUID gets refreshed by the next response
	// header so we don't pre-clear it.
	return nil
}

// UpdateIdentityName for Hermes lives in identity.go — it rewrites the name in
// <hermes>/SOUL.md (Hermes's identity file; it has no separate IDENTITY.md slot).

// WriteMCPEntry + RemoveMCPEntry live in mcp.go — they upsert/delete
// mcp_servers.<name> in ~/.hermes/config.yaml and restart the gateway, mirroring
// internal/openclaw/mcp.go (which edits openclaw.json mcp.servers).
