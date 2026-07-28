package mqtthandler

import (
	"encoding/json"
	"log/slog"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
)

// handleDiscordEvent processes a cmd:"discord_event" from the bff-campaign-service
// Discord relay (managed shared-bot mode): it dedups on the Discord message id,
// type-asserts the active gateway to domain.DiscordBridge, and injects the message
// as a chat turn. The reply is delivered asynchronously via the ChannelRelay
// installed at startup (publishes cmd:"discord_reply" on fd_channel). The
// fd_channel ack mirrors publishSlackResult so the relay's observability stays
// uniform across channels. Dedup reuses the shared event_id LRU (rememberOrSkip);
// Discord message ids and Slack event ids never collide.
func (h *DeviceMQTTHandler) handleDiscordEvent(cmd domain.MQTTMessage) error {
	var ev domain.MQTTDiscordEventCommand
	if err := json.Unmarshal(cmd.Raw(), &ev); err != nil {
		slog.Error("discord_event: invalid payload", "component", "mqtt", "error", err)
		return h.publishDiscordResult("", "failure", "invalid JSON payload")
	}
	if ev.Text == "" {
		slog.Error("discord_event: empty text", "component", "mqtt", "event_id", ev.EventID)
		return h.publishDiscordResult(ev.EventID, "failure", "empty text")
	}
	if rememberOrSkip(ev.EventID) {
		slog.Debug("discord_event: dedup skip", "component", "mqtt", "event_id", ev.EventID)
		return h.publishDiscordResult(ev.EventID, "skipped_duplicate", "")
	}

	// Managed Discord requires the active runtime to be its own bridge frontend
	// (mirrors the SlackBridge assert in forwardSlackHTTP). A runtime without it
	// degrades cleanly: the relay gets channel_not_supported and stops routing.
	bridge, ok := h.agentGateway.(domain.DiscordBridge)
	if !ok {
		slog.Warn("discord_event: runtime does not support managed discord",
			"component", "mqtt", "backend", h.agentGateway.Name(), "event_id", ev.EventID)
		return h.publishDiscordResult(ev.EventID, "failure", "channel_not_supported")
	}

	handled, err := bridge.HandleInboundDiscord(ev.ToInbound())
	if err != nil {
		slog.Error("discord_event: handle failed", "component", "mqtt", "event_id", ev.EventID, "error", err)
		return h.publishDiscordResult(ev.EventID, "failure", err.Error())
	}
	slog.Debug("discord_event: handled", "component", "mqtt", "event_id", ev.EventID, "started_turn", handled)
	return h.publishDiscordResult(ev.EventID, "success", "")
}

// publishDiscordResult mirrors publishSlackResult so the fd_channel ack vocabulary
// stays consistent across forwarded channel events.
func (h *DeviceMQTTHandler) publishDiscordResult(eventID, status, errMsg string) error {
	resp := map[string]any{
		"channel":  domain.ChannelDiscord,
		"type":     domain.CommandDiscordEvent,
		"event_id": eventID,
		"status":   status,
		"error":    errMsg,
		"info":     domain.NewMQTTInfoResponse(h.config, domain.CommandDiscordEvent, device.GetDeviceMac()),
	}
	return h.publish(resp)
}
