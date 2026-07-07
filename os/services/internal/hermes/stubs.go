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
// don't patch it from Device. Returns ErrNotSupportedByRuntime so the caller
// knows nothing was applied (it falls back to EnsureOnboarding, whose presync
// re-syncs llm_base_url/llm_api_key from config.json).
func (s *HermesService) RefreshModelsConfig() error {
	return domain.ErrNotSupportedByRuntime
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

// GetConfigJSON — no agent-side config file under Hermes (config.yaml is owned
// by presync, secrets live in .env). Returns ErrNotSupportedByRuntime so the
// gw-config UI shows "no local config" instead of a misleading empty object.
func (s *HermesService) GetConfigJSON() (json.RawMessage, error) {
	return nil, domain.ErrNotSupportedByRuntime
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

// UpdatePrimaryModel — Hermes ignores the device's primary model: os-server
// sends a fixed request model (constants.go Model) and presync pins
// .model.default to the campaign-api alias, so there is nothing to patch.
func (s *HermesService) UpdatePrimaryModel(_ string) error {
	return domain.ErrNotSupportedByRuntime
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
	slog.Info("CompactSession: not supported (hermes backend)", "component", "hermes", "session", sessionKey)
	return domain.ErrNotSupportedByRuntime
}

// NewSession lives in rotation.go (it rotates the conversation name).

// UpdateIdentityName for Hermes lives in identity.go — it rewrites the name in
// <hermes>/SOUL.md (Hermes's identity file; it has no separate IDENTITY.md slot).

// WriteMCPEntry + RemoveMCPEntry live in mcp.go — they upsert/delete
// mcp_servers.<name> in ~/.hermes/config.yaml and restart the gateway, mirroring
// internal/openclaw/mcp.go (which edits openclaw.json mcp.servers).
