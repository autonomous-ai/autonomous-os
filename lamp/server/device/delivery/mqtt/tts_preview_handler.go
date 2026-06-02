package mqtthandler

import (
	"encoding/json"
	"log/slog"
	"strings"

	"go-lamp.autonomous.ai/domain"
	"go-lamp.autonomous.ai/lib/lelamp"
)

// handleTTSPreview plays a one-shot TTS preview on the lamp without
// persisting any config change. Mirrors the HTTP /api/voice/preview flow:
// API key + base URL are read server-side from config so the BFF never
// has to ship credentials over MQTT. Provider/voice/language overrides
// are optional — empty fields make LeLamp fall back to current config.
func (h *DeviceMQTTHandler) handleTTSPreview(env domain.MQTTDataCommand) error {
	var req domain.MQTTTTSPreviewData
	if err := json.Unmarshal(env.Data, &req); err != nil {
		slog.Error("tts.preview: invalid payload", "component", "mqtt", "error", err)
		return h.publishDataResult(domain.KindTTSPreview, "failure", "invalid JSON payload", nil)
	}

	if strings.TrimSpace(req.Text) == "" {
		slog.Warn("tts.preview: missing text", "component", "mqtt")
		return h.publishDataResult(domain.KindTTSPreview, "failure", "text is required", nil)
	}

	slog.Info("tts.preview: received", "component", "mqtt", "provider", req.Provider, "voice", req.Voice, "language", req.Language)

	// Ack immediately so BFF knows the device received the command.
	if err := h.publishDataResult(domain.KindTTSPreview, "starting", "", nil); err != nil {
		slog.Warn("tts.preview: publish starting ack failed", "component", "mqtt", "error", err)
	}

	go func() {
		apiKey := h.config.GetTTSAPIKey()
		baseURL := h.config.GetTTSBaseURL()
		if err := lelamp.SpeakPreview(req.Text, req.Voice, req.Provider, apiKey, baseURL); err != nil {
			slog.Error("tts.preview: SpeakPreview failed", "component", "mqtt", "error", err)
			if pubErr := h.publishDataResult(domain.KindTTSPreview, "failure", err.Error(), nil); pubErr != nil {
				slog.Warn("tts.preview: publish failure ack failed", "component", "mqtt", "error", pubErr)
			}
			return
		}
		slog.Info("tts.preview: played", "component", "mqtt", "provider", req.Provider, "voice", req.Voice)
		if err := h.publishDataResult(domain.KindTTSPreview, "success", "", nil); err != nil {
			slog.Warn("tts.preview: publish success ack failed", "component", "mqtt", "error", err)
		}
	}()

	return nil
}
