package domain

// DiscordInbound carries one relayed Discord message forwarded by the
// bff-campaign-service Discord relay over MQTT (cmd:"discord_event"). In managed
// mode the device holds no bot token and opens no Gateway session, so the relay
// supplies everything the turn needs — including BotUserID and MentionsBot, which
// a device with a live session would otherwise derive itself.
type DiscordInbound struct {
	EventID        string // Discord message id, used for dedup
	DiscordUserID  string // author id (the allowlist principal)
	GuildID        string // guild id; empty for a DM
	ChannelID      string // channel to reply to
	AuthorUsername string // display name, for the turn text
	BotUserID      string // relay-supplied; device has no session to derive it
	MentionsBot    bool   // relay-supplied; drives the guild requireMention gate
	Text           string // raw content; the device strips the mention itself
}

// ChannelRelay is the outbound sink a managed-Discord runtime uses to reach the
// bff-campaign-service Discord relay (which holds the bot token and does the
// actual Discord REST send). It replaces the on-device discordgo session: instead
// of ChannelMessageSend / ChannelTyping, the runtime publishes over MQTT and the
// relay sends as the bot. os-server provides the MQTT-backed implementation and
// installs it on the active gateway via DiscordBridge.SetChannelRelay.
type ChannelRelay interface {
	// SendDiscordReply publishes a reply for discordChannelID back to the relay.
	// The relay chunks to Discord's 2000-char limit and sends as the bot.
	SendDiscordReply(discordChannelID, text string) error

	// SendDiscordTyping publishes a typing signal for discordChannelID.
	SendDiscordTyping(discordChannelID string) error
}

// DiscordBridge is implemented by runtimes that can run Discord in "managed"
// mode: the shared Autonomous bot token lives only in the cloud relay, never on
// the device, and inbound messages arrive over MQTT (cmd:"discord_event") instead
// of a device-held Gateway session. The discord_event MQTT handler type-asserts
// the active gateway to this interface; runtimes that don't implement it reject
// managed Discord via the ErrChannelNotSupported capability gate (mirrors
// SlackBridge / ChannelSupported).
type DiscordBridge interface {
	// SetChannelRelay installs the outbound reply/typing sink. Called once at
	// startup by os-server, which owns the MQTT publisher. A nil relay leaves the
	// runtime unable to reply (logged when a reply is attempted).
	SetChannelRelay(relay ChannelRelay)

	// HandleInboundDiscord drives one relayed Discord message as a chat turn.
	// handled == false when the allowlist gate rejects it (wrong user/guild, the
	// bot's own message, or a non-actionable event); the caller acks and starts no
	// turn. The reply is delivered later via the ChannelRelay set above.
	HandleInboundDiscord(in DiscordInbound) (handled bool, err error)
}

// DiscordReplyDeliverer is implemented by a DiscordBridge runtime whose channel
// replies are driven by os-server's agent-event handler rather than an internal
// translator (today: hermes). os-server type-asserts the active gateway to this
// interface and, for a discord-origin run, suppresses speaker TTS and delivers
// the final reply to the relay at turn end. Runtimes that finalize their own
// Discord replies internally (claudecode, codex, opencode — their emitFinal calls
// the relay directly) deliberately do NOT implement this, so os-server never
// double-delivers.
type DiscordReplyDeliverer interface {
	// IsDiscordOriginRun reports (without consuming) whether runID came from a
	// relayed Discord event, so the SSE handler suppresses speaker TTS for it.
	IsDiscordOriginRun(runID string) bool

	// DeliverDiscordReply finalizes a discord-origin run: sends the reply to the
	// relay (ChannelRelay.SendDiscordReply) for the run's channel and consumes the
	// origin. A no-op for non-discord runs; empty text just cleans up the origin.
	DeliverDiscordReply(runID string, text string) error
}
