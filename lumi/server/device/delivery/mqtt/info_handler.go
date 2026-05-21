package mqtthandler

import (
	"log/slog"

	agenthttp "go-lamp.autonomous.ai/server/agent/delivery/http"
	"go-lamp.autonomous.ai/domain"
	"go-lamp.autonomous.ai/internal/device"
	"go-lamp.autonomous.ai/lib/lelamp"
)

func (h *DeviceMQTTHandler) handleInfo(_ domain.MQTTMessage) error {
	msg := domain.NewMQTTInfoResponse(h.config, "info", device.GetDeviceMac())
	if v, err := lelamp.GetVersion(); err == nil {
		msg.LelampVersion = v
	}
	msg.OpenClawVersion = agenthttp.GetOpenClawVersion()
	if ip, err := h.networkService.GetCurrentIP(); err == nil {
		msg.LocalIP = ip
	}
	slog.Info("mqtt_handler_info",
		"id", msg.ID,
		"version", msg.Version,
		"lelamp_version", msg.LelampVersion,
		"openclaw_version", msg.OpenClawVersion,
		"local_ip", msg.LocalIP,
		"tts_provider", msg.TTSProvider,
		"tts_voice", msg.TTSVoice,
		"stt_language", msg.STTLanguage,
	)
	return h.publish(msg)
}
