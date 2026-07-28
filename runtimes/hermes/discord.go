package hermes

import (
	"fmt"
	"log/slog"
	"regexp"
	"strings"

	"go.autonomous.ai/os/system/domain"
)

// Managed Discord for the Hermes runtime.
//
// Hermes runs Discord natively inside its own gateway when DISCORD_BOT_TOKEN is
// present in ~/.hermes/.env (presync). In MANAGED mode the device stores no token
// (presync writes none), so the native connection never opens; instead the
// bff-campaign-service Discord relay owns the shared bot and forwards messages
// over MQTT (cmd:"discord_event"). This file is the Hermes half of that path,
// mirroring the Slack HTTP bridge (slack.go): os-server drives the turn via
// HandleInboundDiscord and finalizes the reply via DeliverDiscordReply — but the
// reply goes to the relay (ChannelRelay), never a device-held token.

// discordOrigin records the channel a managed-Discord runID came from so the
// reply can be routed back to it via the relay.
type discordOrigin struct {
	channel string
}

// discordMentionRE strips a leading/any mention of the bot user id.
func discordMentionRE(botUserID string) *regexp.Regexp {
	return regexp.MustCompile(`<@!?` + regexp.QuoteMeta(botUserID) + `>`)
}

// SetChannelRelay implements domain.DiscordBridge — installs the managed reply/typing sink.
func (s *HermesService) SetChannelRelay(relay domain.ChannelRelay) {
	s.discordRelay = relay
}

// HandleInboundDiscord implements domain.DiscordBridge — parses one relayed Discord
// message and starts a Hermes turn for it, recording the channel so DeliverDiscordReply
// can route the reply back. handled == false when the allowlist gate rejects it.
// The relay supplies BotUserID/MentionsBot since the device has no Gateway session.
func (s *HermesService) HandleInboundDiscord(in domain.DiscordInbound) (bool, error) {
	text, ok := s.acceptDiscord(in)
	if !ok {
		slog.Debug("discord: inbound skipped", "component", "hermes",
			"authorId", in.DiscordUserID, "guildId", in.GuildID)
		return false, nil
	}

	reqID, runID := s.NextChatRunID()
	turnText := fmt.Sprintf("[discord] Message from %s (id:%s):\n%s", in.AuthorUsername, in.DiscordUserID, text)
	s.markDiscordOrigin(runID, in.ChannelID)

	if _, err := s.SendChatMessageWithRun(turnText, reqID, runID); err != nil {
		s.consumeDiscordOrigin(runID) // unwind so the map can't leak
		return false, fmt.Errorf("send discord turn to hermes: %w", err)
	}
	slog.Info("discord: forwarded inbound to hermes", "component", "hermes", "channel", in.ChannelID, "run_id", runID)
	return true, nil
}

// acceptDiscord is the pure allowlist gate: closed by default (an explicit
// DiscordUserID is required); a DM is accepted, a guild message must be in the
// allowed guild (when set) and @mention the bot. Returns the mention-stripped text.
func (s *HermesService) acceptDiscord(in domain.DiscordInbound) (string, bool) {
	allowedUser := s.config.DiscordUserID
	allowedGuild := s.config.DiscordGuildID
	if allowedUser == "" || in.DiscordUserID != allowedUser {
		return "", false
	}
	if in.GuildID != "" {
		if allowedGuild != "" && in.GuildID != allowedGuild {
			return "", false
		}
		if !in.MentionsBot {
			return "", false
		}
	}
	text := in.Text
	if in.BotUserID != "" {
		text = discordMentionRE(in.BotUserID).ReplaceAllString(text, "")
	}
	text = strings.TrimSpace(text)
	if text == "" {
		return "", false
	}
	return text, true
}

// markDiscordOrigin records the channel a runID came from.
func (s *HermesService) markDiscordOrigin(runID, channel string) {
	if runID == "" || channel == "" {
		return
	}
	s.discordRunOriginMu.Lock()
	s.discordRunOrigin[runID] = discordOrigin{channel: channel}
	s.discordRunOriginMu.Unlock()
}

// consumeDiscordOrigin returns + clears the origin for runID.
func (s *HermesService) consumeDiscordOrigin(runID string) (discordOrigin, bool) {
	s.discordRunOriginMu.Lock()
	defer s.discordRunOriginMu.Unlock()
	o, ok := s.discordRunOrigin[runID]
	if ok {
		delete(s.discordRunOrigin, runID)
	}
	return o, ok
}

// IsDiscordOriginRun implements domain.DiscordReplyDeliverer — non-consuming peek
// so the SSE handler can suppress speaker TTS for a managed-Discord turn.
func (s *HermesService) IsDiscordOriginRun(runID string) bool {
	if runID == "" {
		return false
	}
	s.discordRunOriginMu.Lock()
	_, ok := s.discordRunOrigin[runID]
	s.discordRunOriginMu.Unlock()
	return ok
}

// DeliverDiscordReply implements domain.DiscordReplyDeliverer — finalizes a
// managed-Discord turn by sending the reply to the relay for the origin channel
// and consuming the origin. No-op for non-Discord runs; empty text just cleans up.
func (s *HermesService) DeliverDiscordReply(runID, text string) error {
	o, ok := s.consumeDiscordOrigin(runID)
	if !ok {
		return nil // not a discord-origin run
	}
	if strings.TrimSpace(text) == "" {
		return nil
	}
	if s.discordRelay == nil {
		return fmt.Errorf("discord managed reply dropped: no relay installed")
	}
	if err := s.discordRelay.SendDiscordReply(o.channel, text); err != nil {
		return fmt.Errorf("send discord reply: %w", err)
	}
	slog.Info("discord reply delivered", "component", "hermes", "channel", o.channel)
	return nil
}
