package openclaw

// slackChannelConfig holds the inputs for applySlackChannelConfig. Grouping them
// in a struct keeps call sites readable (named fields, no positional-arg swaps)
// and lets setup pass only the socket-mode subset while AddChannel adds the
// http-mode proxy fields.
type slackChannelConfig struct {
	BotToken string
	AppToken string // socket mode only
	// UserID is collected for parity with the telegram/discord configs but is NOT
	// used to gate Slack DMs — dmPolicy is always "open" (see applySlackChannelConfig).
	UserID string

	// Mode selects transport: "" / "socket" (default — outbound WSS to Slack, needs
	// AppToken) or "http" (gateway listens for POSTs forwarded from a public proxy
	// via the device's slack_event MQTT handler, needs SigningSecret). The http
	// fields below are only consulted when Mode == "http".
	Mode          string
	SigningSecret string // http mode only
	WebhookPath   string // http mode only; defaults to /slack/events

	// Runtime is the installed openclaw version, used to pick field shapes the
	// runtime accepts (see streaming/socketMode below).
	Runtime RuntimeInfo
}

// applySlackChannelConfig writes the canonical channels.slack block into slackMap.
// Field shape follows the OpenClaw channels.slack canonical example with
// version-aware tweaks driven by cfg.Runtime.AtLeast(2026, 4):
//
//   - `streaming` is picked per runtime: legacy string "partial" on 2026.3.x (object
//     form is hard-rejected on boot); modern object {mode, nativeTransport} on
//     2026.4.x+ (legacy string is hard-rejected on 2026.5.x). Direct write is the
//     only mechanism — there's no post-write doctor --fix to rescue a wrong polarity.
//   - `streaming.nativeTransport: true` is set when writing the object form so Slack's
//     native streaming API delivers progressive text updates (more stable than
//     chunked post/edit when mode=partial).
//   - `socketMode` ping timeouts are seeded via setDefaultValue so any operator
//     overrides under that key are preserved. The block is gated to 2026.4.x+ —
//     older runtimes don't recognise the key.
//
// HTTP mode is the message-loss-tolerant path because Slack retries failed HTTP
// deliveries 3x over ~5 min; Socket Mode drops events sent during disconnects.
func applySlackChannelConfig(slackMap map[string]any, cfg slackChannelConfig) {
	slackMap["enabled"] = true
	slackMap["botToken"] = cfg.BotToken
	// dm.enabled gates message.im delivery; without it 1:1 DMs are silently dropped.
	slackMap["dm"] = map[string]any{
		"enabled":      true,
		"groupEnabled": false,
	}
	slackMap["groupPolicy"] = "open"
	slackMap["ackReaction"] = "eyes"
	slackMap["typingReaction"] = "writing_hand"
	// Runtime-aware streaming (independent of socket/http mode).
	if cfg.Runtime.AtLeast(2026, 4) {
		slackMap["streaming"] = map[string]any{
			"mode":            "partial",
			"nativeTransport": true,
		}
	} else {
		slackMap["streaming"] = "partial"
	}
	slackMap["userTokenReadOnly"] = true
	slackMap["slashCommand"] = map[string]any{
		"enabled":       true,
		"name":          "openclaw",
		"sessionPrefix": "slack:slash",
		"ephemeral":     true,
	}
	// "auto" is off for Slack; must be explicit `true` to enable per-channel native
	// slash commands (/model, /reset, ...).
	slackMap["commands"] = map[string]any{
		"native": true,
	}
	// Always open: bot accepts DMs from any user in the workspace.
	slackMap["dmPolicy"] = "open"
	slackMap["allowFrom"] = mergeStringList(slackMap["allowFrom"], "*")

	// Mode-specific block. Default (empty / "socket") preserves pre-existing
	// behaviour so installs without slack_mode set are byte-identical to the prior
	// canonical shape.
	if cfg.Mode == "http" {
		slackMap["mode"] = "http"
		slackMap["signingSecret"] = cfg.SigningSecret
		webhookPath := cfg.WebhookPath
		if webhookPath == "" {
			webhookPath = "/slack/events"
		}
		slackMap["webhookPath"] = webhookPath
		// HTTP mode does not open a Slack WebSocket — strip Socket-Mode-only keys so
		// the on-disk config converges to the http canonical shape.
		delete(slackMap, "appToken")
		delete(slackMap, "socketMode")
	} else {
		slackMap["mode"] = "socket"
		slackMap["appToken"] = cfg.AppToken
		if cfg.Runtime.AtLeast(2026, 4) {
			// clientPingTimeout=20000 matches the team-validated value that fixes DMs
			// hanging on a stale socket. setDefaultValue preserves operator overrides.
			socketModeMap := ensureMap(slackMap, "socketMode")
			setDefaultValue(socketModeMap, "clientPingTimeout", 20000)
			setDefaultValue(socketModeMap, "serverPingTimeout", 30000)
			slackMap["socketMode"] = socketModeMap
		}
		delete(slackMap, "signingSecret")
		delete(slackMap, "webhookPath")
	}

	// Strip keys that older configs may have left behind so we converge on the
	// canonical shape.
	delete(slackMap, "requireMention")  // per-channel only, not top-level
	delete(slackMap, "nativeStreaming") // legacy alias — superseded by streaming.nativeTransport
}
