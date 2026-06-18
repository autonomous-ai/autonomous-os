package openclaw

// applyDiscordChannelConfig writes the canonical channels.discord block into
// discordMap. Shared by the setup and AddChannel paths so both converge on the
// same shape (mirrors applySlackChannelConfig).
//
// DMs are allowlist-gated on the operator's Discord user id. When a guild id is
// supplied, the bot also answers in that guild (groupPolicy allowlist, no
// mention required) for that same user; without a guild id only DMs are wired.
func applyDiscordChannelConfig(discordMap map[string]any, botToken, userID, guildID string) {
	discordMap["enabled"] = true
	discordMap["dmPolicy"] = "allowlist"
	discordMap["token"] = botToken
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
