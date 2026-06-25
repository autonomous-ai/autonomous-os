package openclaw

import "go.autonomous.ai/os/domain"

// SupportedChannels — OpenClaw runs all four channels: telegram is built-in, slack /
// discord / whatsapp ship as externalized @openclaw/* plugins that AddChannel
// installs on demand (ensureChannelPlugin).
func (s *OpenclawService) SupportedChannels() []string {
	return []string{
		domain.ChannelTelegram,
		domain.ChannelSlack,
		domain.ChannelDiscord,
		domain.ChannelWhatsapp,
	}
}
