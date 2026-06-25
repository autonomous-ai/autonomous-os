package agent

import (
	"context"
	"log/slog"
	"time"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/server/config"
)

// channelReapplyTimeout caps a single channel re-apply. The slow part is the
// gateway restart that openclaw's AddChannel triggers after installing a plugin;
// bound it so one stuck restart can't hang the whole startup reconcile.
const channelReapplyTimeout = 5 * time.Minute

// ChannelReconcile re-applies the configured messaging channels to the active
// runtime after a runtime switch, and records which channels the runtime cannot run.
//
// It mirrors PersonaMigration: it runs once in the startup sequence, is gated by a
// persisted marker (config.ChannelsAppliedRuntime) so it fires only when the runtime
// actually changed, and never blocks startup. Unlike persona migration it runs for
// ANY runtime change (not only adapter-bearing pairs), because channels must be
// re-applied whichever direction the switch went.
//
// The load-bearing case is switching INTO openclaw (its slack/discord plugins are
// installed on demand by AddChannel, so a config-only re-apply would not suffice).
// Switching INTO hermes is largely self-healed already: the presync hook re-syncs
// ~/.hermes/.env before the gateway starts, so hermes.AddChannel here is an
// idempotent no-op (hash-diff finds no change → no restart).
type ChannelReconcile struct {
	cfg *config.Config
	gw  domain.AgentGateway
}

// ProvideChannelReconcile is the Wire provider. It takes the resolved gateway so the
// reconcile re-applies channels against the runtime that is actually active now.
func ProvideChannelReconcile(cfg *config.Config, gw domain.AgentGateway) *ChannelReconcile {
	return &ChannelReconcile{cfg: cfg, gw: gw}
}

// configuredChannels returns one AddChannelRequest per channel that has credentials
// in config.json, so the runtime apply can rebuild it from the persisted creds.
func (r *ChannelReconcile) configuredChannels() []domain.AddChannelRequest {
	c := r.cfg
	var out []domain.AddChannelRequest
	if c.TelegramBotToken != "" {
		out = append(out, domain.AddChannelRequest{
			Channel:          domain.ChannelTelegram,
			TelegramBotToken: c.TelegramBotToken,
			TelegramUserID:   c.TelegramUserID,
		})
	}
	if c.SlackBotToken != "" {
		// Re-apply Slack in HTTP mode (the fleet convention — Socket Mode is not used;
		// see device.Service.RefreshChannelConfig which hardcodes the same). config.json
		// carries no slack-mode field, so without this the request would default to
		// Socket Mode (EffectiveSlackMode) and openclaw would be mis-wired for the
		// proxy-forwarded webhook events after a switch back to openclaw. The HTTP-mode
		// signing secret is the device's llm_api_key, matching what the backend proxy
		// re-signs with.
		out = append(out, domain.AddChannelRequest{
			Channel:            domain.ChannelSlack,
			SlackBotToken:      c.SlackBotToken,
			SlackAppToken:      c.SlackAppToken,
			SlackUserID:        c.SlackUserID,
			SlackMode:          "http",
			SlackSigningSecret: c.LLMAPIKey,
		})
	}
	if c.DiscordBotToken != "" {
		out = append(out, domain.AddChannelRequest{
			Channel:         domain.ChannelDiscord,
			DiscordBotToken: c.DiscordBotToken,
			DiscordGuildID:  c.DiscordGuildID,
			DiscordUserID:   c.DiscordUserID,
		})
	}
	if c.WhatsappUserID != "" {
		out = append(out, domain.AddChannelRequest{
			Channel:        domain.ChannelWhatsapp,
			WhatsappUserID: c.WhatsappUserID,
		})
	}
	return out
}

// Reconcile re-applies the configured channels when the runtime changed since the
// last apply, records the unsupported ones for the info uplink, and advances the
// marker. A no-op when the runtime is unchanged. Never blocks startup; a transient
// apply failure leaves the marker un-advanced so the next boot retries.
func (r *ChannelReconcile) Reconcile() {
	current := r.cfg.AgentRuntime
	if current == "" {
		current = domain.AgentRuntimeOpenClaw
	}
	if r.cfg.ChannelsAppliedRuntime == current {
		return // no switch since channels were last applied
	}

	// First observation (marker never set — e.g. the boot that introduced this
	// field): channels were already applied for the current runtime at setup time,
	// so record the baseline WITHOUT re-applying. Re-applying here would force a
	// gratuitous gateway restart on every device on the upgrade boot. Re-apply only
	// happens on an OBSERVED switch (marker set to a different runtime).
	if r.cfg.ChannelsAppliedRuntime == "" {
		if err := r.cfg.WithLockSave(func(c *config.Config) { c.ChannelsAppliedRuntime = current }); err != nil {
			slog.Warn("channel reconcile: record baseline failed", "component", "agent", "error", err)
			return
		}
		slog.Info("channel reconcile: baseline recorded (no re-apply)", "component", "agent", "runtime", current)
		return
	}

	slog.Info("channel reconcile: runtime changed, re-applying channels",
		"component", "agent", "from", r.cfg.ChannelsAppliedRuntime, "to", current)

	var unsupported []string
	applyErr := false
	for _, req := range r.configuredChannels() {
		if !domain.ChannelSupported(r.gw, req.Channel) {
			slog.Warn("channel not supported on runtime — leaving creds for switch-back",
				"component", "agent", "channel", req.Channel, "runtime", current)
			unsupported = append(unsupported, req.Channel)
			continue
		}
		ctx, cancel := context.WithTimeout(context.Background(), channelReapplyTimeout)
		err := r.gw.AddChannel(ctx, req)
		cancel()
		if err != nil {
			slog.Error("channel re-apply failed; will retry next boot",
				"component", "agent", "channel", req.Channel, "error", err)
			applyErr = true
			continue
		}
		slog.Info("channel re-applied to new runtime",
			"component", "agent", "channel", req.Channel, "runtime", current)
	}

	// Persist the unsupported list and advance the marker ONLY on a clean pass. On a
	// transient apply failure the loop may have `continue`d before reaching the truly
	// unsupported channels, so `unsupported` is incomplete — writing it then would
	// surface a wrong list on the info uplink. Leaving both fields untouched makes the
	// next boot re-run the full reconcile and rebuild the correct list.
	if applyErr {
		slog.Warn("channel reconcile: apply error — leaving marker + unsupported list for next-boot retry",
			"component", "agent", "runtime", current)
		return
	}
	if err := r.cfg.WithLockSave(func(c *config.Config) {
		c.ChannelsUnsupported = unsupported
		c.ChannelsAppliedRuntime = current
	}); err != nil {
		slog.Warn("channel reconcile: persist marker failed", "component", "agent", "error", err)
	}
}
