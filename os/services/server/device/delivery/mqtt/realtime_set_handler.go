package mqtthandler

import (
	"encoding/json"
	"log/slog"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/internal/device"
)

// handleRealtimeSet applies a `realtime.set` downlink — configure the realtime
// voice agent (Gemini Live / OpenAI Realtime). Same flow as tts.set: ack
// immediately, apply async (write config.json + restart hal), then ack the
// outcome. See domain.RealtimeSetData for the full downlink contract the
// FE / BFF push (envelope, fields, valid values, examples).

func (h *DeviceMQTTHandler) publishRealtimeSetAck(status, errMsg string, data *domain.RealtimeSetData) {
	ack := domain.MQTTRealtimeSetAck{
		MQTTInfoResponse: domain.NewMQTTInfoResponse(h.config, "data", device.GetDeviceMac()),
		Kind:             domain.KindRealtimeSet,
		Status:           status,
		Error:            errMsg,
		Data:             data,
	}
	if err := h.publish(ack); err != nil {
		slog.Warn("realtime.set: publish ack failed", "component", "mqtt", "status", status, "error", err)
	}
}

func (h *DeviceMQTTHandler) handleRealtimeSet(env domain.MQTTDataCommand) error {
	var req domain.RealtimeSetData
	if err := json.Unmarshal(env.Data, &req); err != nil {
		slog.Error("realtime.set: invalid payload", "component", "mqtt", "error", err)
		h.publishRealtimeSetAck("failure", "invalid JSON payload", nil)
		return err
	}

	slog.Info("realtime.set: received", "component", "mqtt", "provider", req.Provider, "voice", req.Voice, "reasoning", req.Reasoning)

	// Ack immediately so BFF knows the device received the command.
	h.publishRealtimeSetAck("starting", "", nil)

	go func() {
		if err := h.deviceService.UpdateRealtimeConfig(req); err != nil {
			slog.Error("realtime.set: UpdateRealtimeConfig failed", "component", "mqtt", "error", err)
			h.publishRealtimeSetAck("failure", err.Error(), &req)
			return
		}
		// UpdateRealtimeConfig saves config + kicks systemctl restart hal async.
		// ACK success immediately — BFF doesn't need to wait for hal to come back.
		slog.Info("realtime.set: applied", "component", "mqtt", "provider", req.Provider, "voice", req.Voice)
		h.publishRealtimeSetAck("success", "", &req)
	}()

	return nil
}
