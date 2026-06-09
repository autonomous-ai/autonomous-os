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
		msg.LelampVersion = v
		msg.HalVersion = v
	}
	msg.OpenClawVersion = agenthttp.GetOpenClawVersion()
	if ip, err := h.networkService.GetCurrentIP(); err == nil {
		msg.LocalIP = ip
	}
	slog.Info("mqtt_handler_info",
		"id", msg.ID,
		"version", msg.Version,
		"hal_version", msg.LelampVersion,
		"openclaw_version", msg.OpenClawVersion,
		"local_ip", msg.LocalIP,
		"tts_provider", msg.TTSProvider,
		"tts_voice", msg.TTSVoice,
		"stt_language", msg.STTLanguage,
	)
	return h.publish(msg)
}
