package mqtthandler

import (
	"context"
	"encoding/json"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/mqtt"
	"go.autonomous.ai/os/system/server/config"
)

// mqttChannelRelay is the os-server implementation of domain.ChannelRelay: it
// publishes a managed-Discord runtime's reply/typing back to the
// bff-campaign-service Discord relay on fd_channel, which then sends to Discord
// as the shared bot. It mirrors DeviceMQTTHandler.publish (fresh client per
// send) but is a standalone heap value so it can be installed on the gateway via
// DiscordBridge.SetChannelRelay without depending on the handler's addressability.
type mqttChannelRelay struct {
	config      *config.Config
	mqttFactory *mqtt.Factory
}

func newMQTTChannelRelay(cfg *config.Config, f *mqtt.Factory) *mqttChannelRelay {
	return &mqttChannelRelay{config: cfg, mqttFactory: f}
}

func (r *mqttChannelRelay) publish(data any) error {
	ctx := context.Background()
	mqttClient := r.mqttFactory.GetClient("device-" + r.config.DeviceID)
	if err := mqttClient.Connect(ctx); err != nil {
		return err
	}
	defer mqttClient.Close()
	payload, err := json.Marshal(data)
	if err != nil {
		return err
	}
	return mqttClient.Publish(ctx, r.config.FDChannel, byte(0), payload)
}

// SendDiscordReply publishes the full reply text; the relay chunks to 2000.
func (r *mqttChannelRelay) SendDiscordReply(discordChannelID, text string) error {
	return r.publishDiscordUplink(domain.CommandDiscordReply, discordChannelID, map[string]any{"text": text})
}

// SendDiscordTyping publishes a single typing signal for the channel.
func (r *mqttChannelRelay) SendDiscordTyping(discordChannelID string) error {
	return r.publishDiscordUplink(domain.CommandDiscordTyping, discordChannelID, nil)
}

// publishDiscordUplink builds the managed-Discord fd_channel message on the
// standard device envelope (via NewMQTTInfoResponse: device/type/id/version/mac/
// time) so the relay consumer (stand-to-earn-worker) routes it by top-level
// "type" (SteMessageType) + "device" (device family) + "id" (device id) — the
// same keys as every other device→cloud message. A bare {cmd,...} map is dropped
// as "Received message without device ID" / unhandled. discord_channel_id (+ any
// extra fields like text) ride alongside; "cmd" is retained for legacy consumers.
func (r *mqttChannelRelay) publishDiscordUplink(msgType, discordChannelID string, extra map[string]any) error {
	base := domain.NewMQTTInfoResponse(r.config, msgType, device.GetDeviceMac())
	b, err := json.Marshal(base)
	if err != nil {
		return err
	}
	m := map[string]any{}
	if err := json.Unmarshal(b, &m); err != nil {
		return err
	}
	m["cmd"] = msgType // legacy consumer compatibility (relay routes on "type")
	m["discord_channel_id"] = discordChannelID
	for k, v := range extra {
		m[k] = v
	}
	return r.publish(m)
}
