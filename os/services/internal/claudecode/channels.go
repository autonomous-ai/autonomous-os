package claudecode

import (
	"context"
	"fmt"
	"log/slog"

	"go.autonomous.ai/os/domain"
)

// SupportedChannels — Claude Code runs telegram and discord natively via its
// channel plugins: the bridge launches
// `claude --channels plugin:telegram@claude-plugins-official plugin:discord@...`
// (see https://code.claude.com/docs/en/channels), and each plugin polls its Bot
// API inside the Claude session and replies through the same chat. os-server's
// job is to land the token + allowlist under ~/.claude/channels/<ch>/
// (presync-owned, from config.json) and bounce the bridge — it runs no receive
// loop of its own.
//
// slack is DEVICE-OWNED (HTTP mode): Claude Code has no slack channel plugin —
// "Claude in Slack" is a separate cloud feature that spawns web sessions from
// @Claude mentions, not a device channel. Instead the bff-campaign-service
// proxy fans Slack events out over MQTT to the device's slack_event handler,
// which type-asserts the active gateway to domain.SlackBridge — implemented in
// slack.go (mirror of internal/codex/slack.go), so events route here only
// while claudecode is active. Replies go back via chat.postMessage
// (config.SlackBotToken). whatsapp is OpenClaw-only (Baileys plugin).
func (s *ClaudeCodeService) SupportedChannels() []string {
	return []string{domain.ChannelTelegram, domain.ChannelSlack, domain.ChannelDiscord}
}

// AddChannel — telegram and discord re-sync the channel config from
// config.json (via the embedded presync) and restart the bridge only when
// something actually changed. The device layer persists the channel creds to
// config.json BEFORE calling this (persist-then-apply), so the presync run
// sees them.
//
// slack is an honest no-op success: the device-owned Slack bridge (slack.go)
// reads config.SlackUserID per event and config.SlackBotToken per Web API
// call — fresh from Device config — so the creds the caller just persisted
// are all that is needed; there is nothing agent-side to write and no bridge
// restart. Slack HTTP mode's signing secret is consumed by the public proxy,
// not on the device (the authenticated MQTT path is trusted, same as
// codex/hermes). Unsupported channels return domain.ErrChannelNotSupported.
func (s *ClaudeCodeService) AddChannel(_ context.Context, data domain.AddChannelRequest) error {
	channel := data.EffectiveChannel()
	if !domain.ChannelSupported(s, channel) {
		return fmt.Errorf("claudecode: channel %q: %w", channel, domain.ErrChannelNotSupported)
	}
	if channel == domain.ChannelSlack {
		return nil // creds are read live from Device config (slack.go / slack_sender.go)
	}
	slog.Info("AddChannel: re-syncing channel config", "component", "claudecode", "channel", channel)
	return s.syncChannels()
}

// RefreshChannelConfig — same capability rule and the same apply path as
// AddChannel (both reduce to "re-sync from config.json + restart-if-changed").
// Returns "" for the runtime version string (the active version surfaces via
// Version()).
func (s *ClaudeCodeService) RefreshChannelConfig(_ context.Context, req domain.RefreshChannelRequest) (string, error) {
	if !domain.ChannelSupported(s, req.Channel) {
		return "", fmt.Errorf("claudecode: channel %q: %w", req.Channel, domain.ErrChannelNotSupported)
	}
	if req.Channel == domain.ChannelSlack {
		return "", nil // nothing to refresh — creds are consumed live (see AddChannel)
	}
	slog.Info("RefreshChannelConfig: re-syncing channel config", "component", "claudecode", "channel", req.Channel)
	return "", s.syncChannels()
}

// syncChannels runs the embedded presync (which writes each configured
// channel's token + allowlist into ~/.claude/channels/<ch>/ and the --channels
// launch flags into /root/.claudecode/.env) and restarts the bridge only when
// those files changed — reusing EnsureOnboarding's hash-diff restart logic so
// there is no second copy of the sync/restart rules. Mirrors
// hermes.syncChannelsEnv.
func (s *ClaudeCodeService) syncChannels() error {
	return s.EnsureOnboarding()
}
