package mqtthandler

import (
	"log/slog"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/internal/device"
	"go.autonomous.ai/os/lib/hal"
	agenthttp "go.autonomous.ai/os/server/agent/delivery/http"
)

func (h *DeviceMQTTHandler) handleInfo(_ domain.MQTTMessage) error {
	msg := domain.NewMQTTInfoResponse(h.config, "info", device.GetDeviceMac())
	if v, err := hal.GetVersion(); err == nil {
		msg.HalVersion = v
	}
	msg.OpenClawVersion = agenthttp.GetOpenClawVersion()
	// hermes_version sits next to openclaw_version (both probed at startup); the
	// active one is named by agent_runtime.
	msg.HermesVersion = agenthttp.GetHermesVersion()
	msg.AgentRuntime = device.CurrentAgentRuntimeFromConfig(h.config)
	// Channels configured here that the active runtime can't run (set by
	// ChannelReconcile after a runtime switch, e.g. slack after switching to picoclaw).
	msg.UnsupportedChannels = h.config.ChannelsUnsupported
	if ip, err := h.networkService.GetCurrentIP(); err == nil {
		msg.LocalIP = ip
	}
	slog.Info("mqtt_handler_info",
		"id", msg.ID,
		"version", msg.Version,
		"hal_version", msg.HalVersion,
		"openclaw_version", msg.OpenClawVersion,
		"hermes_version", msg.HermesVersion,
		"agent_runtime", msg.AgentRuntime,
		"local_ip", msg.LocalIP,
		"tts_provider", msg.TTSProvider,
		"tts_voice", msg.TTSVoice,
		"stt_language", msg.STTLanguage,
	)
	return h.publish(msg)
}
