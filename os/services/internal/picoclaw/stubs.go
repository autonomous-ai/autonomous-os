package picoclaw

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"

	"go.autonomous.ai/os/domain"
)

// AddChannel + RefreshChannelConfig + SupportedChannels live in channels.go —
// PicoClaw runs telegram only (device-owned receive loop); slack/discord/whatsapp
// return domain.ErrChannelNotSupported.

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

// ResetAgent lives in reset.go — PicoClaw's factory-reset wipe (stop+disable
// gateway → rm -rf /root/.picoclaw → picoclaw onboard), so it does real work.

// RestartAgent restarts the picoclaw gateway via systemctl so callers that need a
// full gateway reload (config/workspace re-read) get it. Delegates to
// restartPicoclawGateway (service_gateway.go), which no-ops gracefully when
// systemctl is unavailable (non-root / dev box).
func (s *PicoclawService) RestartAgent() error {
	slog.Info("RestartAgent: restarting picoclaw gateway", "component", "picoclaw")
	return restartPicoclawGateway()
}

// RefreshModelsConfig — PicoClaw model config is owned by install.sh/presync.sh
// (switch-runtime flow); we don't patch it from Device.
func (s *PicoclawService) RefreshModelsConfig() error {
	return domain.ErrNotSupportedByRuntime
}

// EnsureOnboarding lives in onboarding.go — it keeps the OS-managed block in the
// workspace AGENTS.md current (the rest of provisioning is owned by install.sh /
// presync.sh). Kept out of this stub file because it does real work.

// FetchChatHistory — PicoClaw history is server-side and we don't walk it.
// Returns empty so callers degrade gracefully (also keeps the read loop's
// synchronous dispatch deadlock-free since the handler never blocks on a WS RPC).
func (s *PicoclawService) FetchChatHistory(_ string, _ int) (json.RawMessage, error) {
	return nil, nil
}

// GetConfigJSON returns the raw bytes of PicoClaw's config.json (the structure
// file: agents/model_list/gateway/channel_list/tools — secrets live in .security.yml,
// which we never expose). Read-only; feeds the gw-config debug UI. Path helper +
// MCP writers live in mcp.go.
func (s *PicoclawService) GetConfigJSON() (json.RawMessage, error) {
	data, err := os.ReadFile(picoclawConfigPath())
	if err != nil {
		return nil, fmt.Errorf("read picoclaw config.json: %w", err)
	}
	return json.RawMessage(data), nil
}

// WatchIdentity + UpdateIdentityName live in identity.go — PicoClaw's IDENTITY.md
// is a 1-for-1 copy of OpenClaw's (same format), so it watches/rewrites the
// `**Name:**` card line just like OpenClaw does.

// StartSkillWatcher lives in skill_watcher.go — it polls OTA metadata and
// auto-updates workspace skills from the CDN (capability-gated), mirroring openclaw.

// StartModelSync — model registry is owned by PicoClaw. No-op.
func (s *PicoclawService) StartModelSync(ctx context.Context) {
	<-ctx.Done()
}

// UpdatePrimaryModel — the PicoClaw model registry (config.json model_list) is
// owned by the runtime's own provisioning, not device-selectable.
func (s *PicoclawService) UpdatePrimaryModel(_ string) error {
	return domain.ErrNotSupportedByRuntime
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

// CompactSession — PicoClaw does not expose a compact API; rotate via
// NewSession instead.
func (s *PicoclawService) CompactSession(sessionKey string) error {
	slog.Info("CompactSession: not supported (picoclaw backend)", "component", "picoclaw", "session", sessionKey)
	return domain.ErrNotSupportedByRuntime
}

const rotateCompressRatioPercent = 75
const picoclawFallbackTokenThreshold = 150_000

// ShouldRotateSession rotates on real session token count (see domain.AgentGateway).
func (s *PicoclawService) ShouldRotateSession(totalTokens, _ int) bool {
	if compressAt := int(s.lastCompressAt.Load()); compressAt > 0 {
		return totalTokens >= compressAt*rotateCompressRatioPercent/100
	}
	return totalTokens > picoclawFallbackTokenThreshold
}

// NewSession — PicoClaw has no sessions.new RPC. Dropping the local session id
// makes the next turn start a fresh server-side session.
func (s *PicoclawService) NewSession(sessionKey string) error {
	slog.Info("NewSession: clearing session (picoclaw backend)", "component", "picoclaw", "key", sessionKey)
	s.sessionUUID.Store("")
	return nil
}

// WriteMCPEntry + RemoveMCPEntry live in mcp.go — PicoClaw writes tools.mcp.servers
// in config.json (nested, gated by tools.mcp.enabled), so they do real work.
