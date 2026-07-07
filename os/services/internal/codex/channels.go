package codex

import (
	"context"
	"fmt"

	"go.autonomous.ai/os/domain"
)

// Channels are DEVICE-OWNED under Codex. The Codex CLI has no channel layer
// of its own (unlike picoclaw, whose runtime binary polls the Telegram Bot API
// itself — see internal/picoclaw/presync.sh), so os-server runs both inbound
// paths:
//   - telegram: the getUpdates receive loop (telegram_poll.go, one goroutine
//     started from StartWS — it lives inside this service's lifecycle, so it
//     runs ONLY while codex is the active runtime and can never fight another
//     runtime's poller; Telegram 409s concurrent pollers, the hermes lesson).
//   - slack (HTTP mode): the bff-campaign-service proxy fans Slack events out
//     over MQTT to the device's slack_event handler, which type-asserts the
//     active gateway to domain.SlackBridge — implemented in slack.go, so
//     events route here only while codex is active. Replies go back via
//     chat.postMessage (config.SlackBotToken).
//   - discord: a Gateway WebSocket bot session (discordgo, discord.go) started
//     from StartWS like the telegram loop — active-runtime-only, so it never
//     fights another runtime's bot session for the same token. DMs from the
//     allowlisted user and @mentions in the configured guild become turns;
//     replies go back via ChannelMessageSend.
//
// Whatsapp has no receive path and stays unsupported.

// SupportedChannels — telegram (device-owned receive loop, telegram_poll.go),
// slack (HTTP-mode proxy path, slack.go) and discord (gateway bot session,
// discord.go).
func (s *CodexService) SupportedChannels() []string {
	return []string{domain.ChannelTelegram, domain.ChannelSlack, domain.ChannelDiscord}
}

// AddChannel — telegram, slack and discord are honest no-op successes: every
// consumer reads the creds fresh from Device config on each use (the telegram
// loop reads config.TelegramBotToken / TelegramUserID per iteration; the
// Slack bridge reads config.SlackUserID per event and config.SlackBotToken
// per Web API call; the discord bot reads config.DiscordBotToken per connect
// attempt and DiscordUserID / DiscordGuildID per message), so the creds the
// caller just persisted to config.json are all that is needed — there is
// nothing agent-side to write. Slack HTTP mode's signing secret is consumed
// by the public proxy, not on the device (same as hermes' bridge: the
// authenticated MQTT path is trusted). Discord additionally validates that
// the request carries the two creds the receive path cannot work without
// (bot token + allowlisted user id — the accept filter is closed when the
// allowlist is empty, so accepting without them would be fake success).
// Whatsapp has no receive path and returns domain.ErrChannelNotSupported.
func (s *CodexService) AddChannel(_ context.Context, data domain.AddChannelRequest) error {
	channel := data.EffectiveChannel()
	if !domain.ChannelSupported(s, channel) {
		return fmt.Errorf("codex: channel %q: %w", channel, domain.ErrChannelNotSupported)
	}
	if channel == domain.ChannelDiscord {
		if data.DiscordBotToken == "" {
			return fmt.Errorf("codex: discord_bot_token is required")
		}
		if data.DiscordUserID == "" {
			return fmt.Errorf("codex: discord_user_id is required")
		}
	}
	return nil
}

// RefreshChannelConfig — same rule: telegram/slack/discord creds are consumed
// live from Device config, so there is nothing to refresh and no derived
// value to return. Other channels are not supported.
func (s *CodexService) RefreshChannelConfig(_ context.Context, req domain.RefreshChannelRequest) (string, error) {
	if !domain.ChannelSupported(s, req.Channel) {
		return "", fmt.Errorf("codex: channel %q: %w", req.Channel, domain.ErrChannelNotSupported)
	}
	return "", nil
}
