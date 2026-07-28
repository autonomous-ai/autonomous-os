package openclaw

// applyDiscordChannelConfig writes the canonical channels.discord block into
// discordMap. Shared by the setup and AddChannel paths so both converge on the
// same shape (mirrors applySlackChannelConfig).
//
// DMs are allowlist-gated on the operator's Discord user id. When a guild id is
// supplied, the bot also answers in that guild (groupPolicy allowlist, no
// mention required) for that same user; without a guild id only DMs are wired.
//
// managed selects the shared-bot relay path (domain.DiscordBridge): the token
// lives only in the cloud relay, never on the device, and inbound events arrive
// over MQTT (cmd:"discord_event") rather than a device-held Gateway session. In
// managed mode the native @openclaw/discord block is disabled (enabled=false) and
// the token is OMITTED entirely so the plugin can never open a session — os-server
// owns the turn (HandleInboundDiscord) and the reply (DeliverDiscordReply → relay).
// The non-managed path is byte-for-byte unchanged (managed=false → enabled=true +
// token written, as before).
func applyDiscordChannelConfig(discordMap map[string]any, botToken, userID, guildID string, managed bool) {
	discordMap["enabled"] = !managed
	discordMap["dmPolicy"] = "allowlist"
	if !managed {
		discordMap["token"] = botToken
	}
	discordMap["allowFrom"] = mergeStringList(discordMap["allowFrom"], userID)
	if guildID != "" {
		discordMap["groupPolicy"] = "allowlist"
		discordMap["guilds"] = map[string]any{
			guildID: map[string]any{
				"requireMention": false,
				"users":          []string{userID},
			},
		}
	}
}
