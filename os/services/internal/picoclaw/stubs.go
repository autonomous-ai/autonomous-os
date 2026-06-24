package picoclaw

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"

	"go.autonomous.ai/os/domain"
)

// SetupAgent — PicoClaw is assumed already running on the Pi as a systemd
// service with skills provisioned externally (see docs/agentic/picoclaw.md). This is a
// no-op so the setup flow doesn't try to write a config / restart a gateway.
func (s *PicoclawService) SetupAgent(_ domain.SetupRequest) error {
	slog.Info("SetupAgent: no-op (picoclaw backend)", "component", "picoclaw")
	return nil
}

// AddChannel — channels run inside Device (Telegram receive loop) when on
// PicoClaw, not as plugins inside the agent runtime. No-op here; channel
// credentials live in the regular Device config (TelegramBotToken, etc.).
func (s *PicoclawService) AddChannel(_ context.Context, _ domain.AddChannelRequest) error {
	slog.Info("AddChannel: no-op (picoclaw backend)", "component", "picoclaw")
	return nil
}

// RefreshChannelConfig — PicoClaw owns its own config layout; not supported here.
func (s *PicoclawService) RefreshChannelConfig(_ context.Context, _ domain.RefreshChannelRequest) (string, error) {
	slog.Info("RefreshChannelConfig: not supported (picoclaw backend)", "component", "picoclaw")
	return "", fmt.Errorf("channel refresh not supported on picoclaw backend")
}

func (s *PicoclawService) HasWhatsappSession(_ string) bool { return false }

// PairWhatsapp — WhatsApp pairing requires a Baileys-style plugin which lives
// only in OpenClaw. Returns a one-shot failure event so the caller's drain loop
// exits cleanly.
func (s *PicoclawService) PairWhatsapp(_ context.Context) <-chan domain.PairingEvent {
	ch := make(chan domain.PairingEvent, 1)
	ch <- domain.PairingEvent{
		Status: domain.PairingStatusFailure,
		Error:  "whatsapp pairing not supported on picoclaw backend",
	}
	close(ch)
	return ch
}

func (s *PicoclawService) ResetAgent() error {
	slog.Info("ResetAgent: no-op (picoclaw backend)", "component", "picoclaw")
	return nil
}

func (s *PicoclawService) RestartAgent() error {
	slog.Info("RestartAgent: no-op (picoclaw backend — manage via systemctl externally)", "component", "picoclaw")
	return nil
}

// RefreshModelsConfig — PicoClaw config is owned externally; we don't patch it.
func (s *PicoclawService) RefreshModelsConfig() error {
	return nil
}

// EnsureOnboarding — PicoClaw is provisioned externally with skills and soul.
// No-op so the os-server boot path stays generic.
func (s *PicoclawService) EnsureOnboarding() error {
	return nil
}

// FetchChatHistory — PicoClaw history is server-side and we don't walk it.
// Returns empty so callers degrade gracefully (also keeps the read loop's
// synchronous dispatch deadlock-free since the handler never blocks on a WS RPC).
func (s *PicoclawService) FetchChatHistory(_ string, _ int) (json.RawMessage, error) {
	return nil, nil
}

// GetConfigJSON — no agent-side config file under PicoClaw. Returns empty.
func (s *PicoclawService) GetConfigJSON() (json.RawMessage, error) {
	return json.RawMessage(`{}`), nil
}

// WatchIdentity — IDENTITY.md / wake-word rename watching is OpenClaw-specific.
// Under PicoClaw, prompts are owned by the PicoClaw server. No-op so the existing
// goroutine slot in server.go stays valid.
func (s *PicoclawService) WatchIdentity(ctx context.Context) {
	<-ctx.Done()
}

// StartSkillWatcher — skills are pre-provisioned on the PicoClaw box. No-op.
func (s *PicoclawService) StartSkillWatcher(ctx context.Context) {
	<-ctx.Done()
}

// StartModelSync — model registry is owned by PicoClaw. No-op.
func (s *PicoclawService) StartModelSync(ctx context.Context) {
	<-ctx.Done()
}

func (s *PicoclawService) UpdatePrimaryModel(_ string) error {
	return nil
}

// StartPrimaryModelWatch — no agent-side config file to watch.
func (s *PicoclawService) StartPrimaryModelWatch(ctx context.Context) {
	<-ctx.Done()
}

// GetConfiguredChannel — Device config is the source of truth under PicoClaw.
// Returns "telegram" when a bot token is set, otherwise the generic label.
func (s *PicoclawService) GetConfiguredChannel() string {
	if s.config.TelegramBotToken != "" {
		return "telegram"
	}
	return "channel"
}

// CompactSession — PicoClaw does not expose a compact API. No-op.
func (s *PicoclawService) CompactSession(sessionKey string) error {
	slog.Info("CompactSession: no-op (picoclaw backend)", "component", "picoclaw", "session", sessionKey)
	return nil
}

// NewSession — PicoClaw has no sessions.new RPC. Dropping the local session id
// makes the next turn start a fresh server-side session.
func (s *PicoclawService) NewSession(sessionKey string) error {
	slog.Info("NewSession: clearing session (picoclaw backend)", "component", "picoclaw", "key", sessionKey)
	s.sessionUUID.Store("")
	return nil
}

// UpdateIdentityName — under PicoClaw, IDENTITY.md is owned by the external
// PicoClaw server, not Device. No-op.
func (s *PicoclawService) UpdateIdentityName(_ string) error {
	slog.Info("UpdateIdentityName: no-op (picoclaw backend)", "component", "picoclaw")
	return nil
}

// WriteMCPEntry — MCP connector writes are an OpenClaw-only feature today.
// No-op so the AgentGateway interface is satisfied.
func (s *PicoclawService) WriteMCPEntry(_ string, _ map[string]any) error {
	slog.Info("WriteMCPEntry: no-op (picoclaw backend)", "component", "picoclaw")
	return nil
}

// RemoveMCPEntry — pairs with WriteMCPEntry. Returns removed=false so callers
// treat it as "entry already absent" — idempotent no-op, no restart triggered.
func (s *PicoclawService) RemoveMCPEntry(_ string) (bool, error) {
	slog.Info("RemoveMCPEntry: no-op (picoclaw backend)", "component", "picoclaw")
	return false, nil
}
