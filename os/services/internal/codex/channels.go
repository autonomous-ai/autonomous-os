package codex

import (
	"context"
	"fmt"

	"go.autonomous.ai/os/domain"
)

// TODO(codex-telegram): Codex has NO inbound messaging channel today. The Codex
// CLI has no channel layer of its own (unlike picoclaw, whose runtime binary
// polls the Telegram Bot API itself — see internal/picoclaw/presync.sh, which
// enables channel_list.telegram + token in picoclaw's own config), and os-server
// runs no Telegram receive loop either. The earlier "telegram is device-owned"
// claim copied from picoclaw was false. Until someone builds an inbound loop
// (os-server-side getUpdates poller, or a channel layer in the codex gatewayd),
// no channel is supported.

// SupportedChannels — none. See TODO(codex-telegram) above. Outbound-only
// delivery (proactive sensing alerts via the Bot API) still works through
// TelegramSender when config.TelegramBotToken is set, but that is not an
// inbound channel and must not be advertised as one.
func (s *CodexService) SupportedChannels() []string {
	return nil
}

// AddChannel — every channel, telegram included, returns
// domain.ErrChannelNotSupported: there is no receive loop to wire the creds
// into, so a no-op success here would be fake (the user would believe the bot
// listens when nothing does).
func (s *CodexService) AddChannel(_ context.Context, data domain.AddChannelRequest) error {
	return fmt.Errorf("codex: channel %q: %w", data.EffectiveChannel(), domain.ErrChannelNotSupported)
}

// RefreshChannelConfig — same rule: nothing consumes channel config under
// Codex, so every channel is not supported.
func (s *CodexService) RefreshChannelConfig(_ context.Context, req domain.RefreshChannelRequest) (string, error) {
	return "", fmt.Errorf("codex: channel %q: %w", req.Channel, domain.ErrChannelNotSupported)
}
