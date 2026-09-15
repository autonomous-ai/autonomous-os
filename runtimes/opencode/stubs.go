package opencode

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"

	"go.autonomous.ai/os/system/domain"
)

// AddChannel + RefreshChannelConfig + SupportedChannels live in channels.go —
// telegram is device-owned (os-server runs the getUpdates receive loop, see
// telegram_poll.go); slack/discord/whatsapp return domain.ErrChannelNotSupported.

func (s *OpenCodeService) HasWhatsappSession(_ string) bool { return false }

// PairWhatsapp — WhatsApp pairing requires a Baileys-style plugin which lives
// only in OpenClaw. Returns a one-shot failure event so the caller's drain loop
// exits cleanly.
func (s *OpenCodeService) PairWhatsapp(_ context.Context) <-chan domain.PairingEvent {
	ch := make(chan domain.PairingEvent, 1)
	ch <- domain.PairingEvent{
		Status: domain.PairingStatusFailure,
		Error:  "whatsapp pairing not supported on opencode backend",
	}
	close(ch)
	return ch
}

// ResetAgent lives in reset.go — OpenCode's factory-reset wipe (stop+disable
// gateway → rm -rf /root/.opencode + XDG config/data → recreate baseline dirs;
// presync re-asserts opencode.json/.env on the next switch), so it does real work.

// RestartAgent restarts the opencode gateway via systemctl so callers that need a
// full gateway reload (config/workspace re-read) get it. Delegates to
// restartOpenCodeGateway (service_gateway.go), which no-ops gracefully when
// systemctl is unavailable (non-root / dev box).
func (s *OpenCodeService) RestartAgent() error {
	slog.Info("RestartAgent: restarting opencode gateway", "component", "opencode")
	return restartOpenCodeGateway()
}

// RefreshModelsConfig — OpenCode model config (opencode.json model/provider/
// apiKey) is owned by presync.sh. Returns ErrNotSupportedByRuntime so the caller
// falls back to EnsureOnboarding, whose presync re-reads llm_* from
// config.json and the hash gate restarts the gateway — i.e. the change is
// applied, just not by this method.
func (s *OpenCodeService) RefreshModelsConfig() error {
	return domain.ErrNotSupportedByRuntime
}

// EnsureOnboarding lives in onboarding.go — it keeps the OS-managed block in the
// workspace AGENTS.md current (the rest of provisioning is owned by install.sh /
// presync.sh). Kept out of this stub file because it does real work.

// FetchChatHistory — OpenCode history is server-side and we don't walk it.
// Returns empty so callers degrade gracefully (also keeps the read loop's
// synchronous dispatch deadlock-free since the handler never blocks on a WS RPC).
func (s *OpenCodeService) FetchChatHistory(_ string, _ int) (json.RawMessage, error) {
	return nil, nil
}

// GetConfigJSON returns opencode's global config (~/.config/opencode/opencode.json)
// verbatim for the gw-config UI. Unlike codex (TOML) this is real JSON and safe
// to expose — the provider apiKey is a `{env:LLM_API_KEY}` reference, the real
// secret lives only in .env. Returns ErrNotSupportedByRuntime when the file is
// absent (un-provisioned) so the UI reports "no device-side config file".
func (s *OpenCodeService) GetConfigJSON() (json.RawMessage, error) {
	raw, err := os.ReadFile(opencodeConfigJSON)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, domain.ErrNotSupportedByRuntime
		}
		return nil, fmt.Errorf("read opencode config: %w", err)
	}
	if !json.Valid(raw) {
		return nil, fmt.Errorf("opencode config is not valid JSON")
	}
	return json.RawMessage(raw), nil
}

// WatchIdentity + UpdateIdentityName live in identity.go — OpenCode's IDENTITY.md
// is a 1-for-1 copy of OpenClaw's (same format), so it watches/rewrites the
// `**Name:**` card line just like OpenClaw does.

// StartSkillWatcher lives in skill_watcher.go — it polls OTA metadata and
// auto-updates opencode's global skills (~/.config/opencode/skills) from the
// CDN (capability-gated), mirroring openclaw.

// StartModelSync — model registry is owned by OpenCode. No-op.
func (s *OpenCodeService) StartModelSync(ctx context.Context) (runtimeErr error) {
	<-ctx.Done()

	return
}

// UpdatePrimaryModel — the OpenCode model is pinned in opencode.json by presync
// (from config.json llm_model), not patched per-call. Same fallback contract
// as RefreshModelsConfig: sentinel → EnsureOnboarding presync applies it.
func (s *OpenCodeService) UpdatePrimaryModel(_ string) error {
	return domain.ErrNotSupportedByRuntime
}

// StartPrimaryModelWatch — no agent-side config file to watch.
func (s *OpenCodeService) StartPrimaryModelWatch(ctx context.Context) (runtimeErr error) {
	<-ctx.Done()

	return
}

// GetConfiguredChannel — Device config is the source of truth under OpenCode.
// Returns "telegram" when a bot token is set, otherwise the generic label.
func (s *OpenCodeService) GetConfiguredChannel() string {
	if s.config.TelegramBotToken != "" {
		return "telegram"
	}
	return "channel"
}

// CompactSession + ShouldRotateSession + NewSession live in rotation.go —
// the session-lifecycle policy (fallback token threshold, session.new frame).

// WriteMCPEntry + RemoveMCPEntry live in mcp.go — OpenCode keeps MCP servers in
// opencode.json under the "mcp" object, so they do real work.
