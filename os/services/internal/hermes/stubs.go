package hermes

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"

	"go.autonomous.ai/os/domain"
)

// SetupAgent — Hermes is assumed already running on the Pi with skills
// provisioned externally (see hermes.md §10). This is a no-op so the setup
// flow doesn't try to write openclaw.json / restart a gateway.
func (s *Service) SetupAgent(_ domain.SetupRequest) error {
	slog.Info("SetupAgent: no-op (hermes backend)", "component", "hermes")
	return nil
}

// AddChannel — channels run inside Lumi (Telegram receive loop) when on
// Hermes, not as plugins inside the agent runtime. No-op here; channel
// credentials live in the regular Lumi config (TelegramBotToken, etc.).
func (s *Service) AddChannel(_ context.Context, _ domain.AddChannelRequest) error {
	slog.Info("AddChannel: no-op (hermes backend)", "component", "hermes")
	return nil
}

// RefreshChannelConfig — refresh re-applies channels.<channel> in openclaw.json,
// but Hermes owns its own ~/.hermes config layout. Not supported on this backend.
func (s *Service) RefreshChannelConfig(_ context.Context, _ domain.RefreshChannelRequest) (string, error) {
	slog.Info("RefreshChannelConfig: not supported (hermes backend)", "component", "hermes")
	return "", fmt.Errorf("channel refresh not supported on hermes backend")
}

func (s *Service) HasWhatsappSession(_ string) bool { return false }

// PairWhatsapp — WhatsApp pairing requires a Baileys-style plugin which lives
// only in OpenClaw. Returns a one-shot failure event so the caller's drain
// loop exits cleanly.
func (s *Service) PairWhatsapp(_ context.Context) <-chan domain.PairingEvent {
	ch := make(chan domain.PairingEvent, 1)
	ch <- domain.PairingEvent{
		Status: domain.PairingStatusFailure,
		Error:  "whatsapp pairing not supported on hermes backend",
	}
	close(ch)
	return ch
}

func (s *Service) ResetAgent() error {
	slog.Info("ResetAgent: no-op (hermes backend)", "component", "hermes")
	return nil
}

func (s *Service) RestartAgent() error {
	slog.Info("RestartAgent: no-op (hermes backend — manage via systemctl externally)", "component", "hermes")
	return nil
}

// RefreshModelsConfig — Hermes config (~/.hermes/...) is owned externally; we
// don't patch it from Lumi. No-op.
func (s *Service) RefreshModelsConfig() error {
	return nil
}

// EnsureOnboarding — user has confirmed Hermes is provisioned with skills and
// soul. No-op so the os-server boot path stays generic.
func (s *Service) EnsureOnboarding() error {
	return nil
}

// FetchChatHistory — Hermes per-conversation history is server-side, but we
// don't currently walk the previous_response_id chain (hermes.md §17 decided
// "conversation name is enough"). Returns empty so callers degrade gracefully.
func (s *Service) FetchChatHistory(_ string, _ int) (json.RawMessage, error) {
	return nil, nil
}

// GetConfigJSON — no agent-side config file under Hermes. Returns empty.
func (s *Service) GetConfigJSON() (json.RawMessage, error) {
	return json.RawMessage(`{}`), nil
}

// WatchIdentity — IDENTITY.md / wake-word rename watching is OpenClaw-specific
// (it pushes the new word into the agent's prompt). Under Hermes, prompts are
// owned by the Hermes server. No-op so the existing goroutine slot in
// server.go stays valid.
func (s *Service) WatchIdentity(ctx context.Context) {
	<-ctx.Done()
}

// StartSkillWatcher — skills are pre-provisioned on the Hermes box (per user).
// No download/notify loop. No-op.
func (s *Service) StartSkillWatcher(ctx context.Context) {
	<-ctx.Done()
}

// StartModelSync — model registry is owned by Hermes. No-op.
func (s *Service) StartModelSync(ctx context.Context) {
	<-ctx.Done()
}

func (s *Service) UpdatePrimaryModel(_ string) error {
	return nil
}

// StartPrimaryModelWatch — no openclaw.json to watch.
func (s *Service) StartPrimaryModelWatch(ctx context.Context) {
	<-ctx.Done()
}

// GetConfiguredChannel — Lumi config is the source of truth under Hermes.
// Returns "telegram" when a bot token is set, otherwise the generic label.
func (s *Service) GetConfiguredChannel() string {
	if s.config.TelegramBotToken != "" {
		return "telegram"
	}
	return "channel"
}

// CompactSession — Hermes does not currently expose a compact API or CLI
// (hermes.md §7 decided to no-op). Workaround: rotate the conversation name
// via NewSession when context grows too large.
func (s *Service) CompactSession(sessionKey string) error {
	slog.Info("CompactSession: no-op (hermes backend)", "component", "hermes", "session", sessionKey)
	return nil
}

// NewSession — under Hermes, "new session" means routing future turns to a
// fresh named conversation. Setting an empty key restores the default. We do
// not delete prior history (Hermes server still has it under the old name).
func (s *Service) NewSession(sessionKey string) error {
	slog.Info("NewSession: rotating conversation (hermes backend)", "component", "hermes", "key", sessionKey)
	// sessionKey here is treated as the next conversation name. Empty means
	// reset to default. The session UUID gets refreshed by the next response
	// header so we don't pre-clear it.
	return nil
}

// TODO(hermes-identity): implement UpdateIdentityName once Hermes has an
// identity surface (wake word / personality name). Currently bypassed — under
// Hermes, IDENTITY.md is owned by the external Hermes server, not Lumi.
func (s *Service) UpdateIdentityName(_ string) error {
	slog.Info("UpdateIdentityName: no-op (hermes backend — TODO)", "component", "hermes")
	return nil
}

// TODO(hermes-mcp): implement WriteMCPEntry once Hermes exposes an MCP
// connector config surface. Currently bypassed so the AgentGateway interface
// is satisfied — MCP connector writes are an OpenClaw-only feature today.
func (s *Service) WriteMCPEntry(_ string, _ map[string]any) error {
	slog.Info("WriteMCPEntry: no-op (hermes backend — TODO)", "component", "hermes")
	return nil
}

// TODO(hermes-mcp): implement RemoveMCPEntry alongside WriteMCPEntry. Returns
// removed=false so callers (mcp_connector_writer) treat it as "entry already
// absent" — idempotent no-op, no restart triggered.
func (s *Service) RemoveMCPEntry(_ string) (bool, error) {
	slog.Info("RemoveMCPEntry: no-op (hermes backend — TODO)", "component", "hermes")
	return false, nil
}
