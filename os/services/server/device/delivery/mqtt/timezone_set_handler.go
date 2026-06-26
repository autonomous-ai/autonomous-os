package mqtthandler

import (
	"encoding/json"
	"log/slog"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/internal/device"
)

// handleTimezoneSet applies a `timezone.set` downlink — set the device's IANA
// timezone. Same flow as realtime.set: ack immediately, apply async (rewrite
// /etc/localtime + /etc/timezone, persist config.json), then ack the outcome.
// Takes effect without a HAL restart (HAL's clock helpers read /etc/timezone
// fresh per call). See domain.TimezoneSetData for the downlink contract.

func (h *DeviceMQTTHandler) publishTimezoneSetAck(status, errMsg string, data *domain.TimezoneSetData) {
	ack := domain.MQTTTimezoneSetAck{
		MQTTInfoResponse: domain.NewMQTTInfoResponse(h.config, "data", device.GetDeviceMac()),
		Kind:             domain.KindTimezoneSet,
		Status:           status,
		Error:            errMsg,
		Data:             data,
	}
	if err := h.publish(ack); err != nil {
		slog.Warn("timezone.set: publish ack failed", "component", "mqtt", "status", status, "error", err)
	}
}

func (h *DeviceMQTTHandler) handleTimezoneSet(env domain.MQTTDataCommand) error {
	var req domain.TimezoneSetData
	if err := json.Unmarshal(env.Data, &req); err != nil {
		slog.Error("timezone.set: invalid payload", "component", "mqtt", "error", err)
		h.publishTimezoneSetAck("failure", "invalid JSON payload", nil)
		return err
	}

	slog.Info("timezone.set: received", "component", "mqtt", "timezone", req.Timezone)

	// Ack immediately so BFF knows the device received the command.
	h.publishTimezoneSetAck("starting", "", nil)

	go func() {
		if err := h.deviceService.SetTimezone(req.Timezone); err != nil {
			slog.Error("timezone.set: SetTimezone failed", "component", "mqtt", "error", err)
			h.publishTimezoneSetAck("failure", err.Error(), &req)
			return
		}
		slog.Info("timezone.set: applied", "component", "mqtt", "timezone", req.Timezone)
		h.publishTimezoneSetAck("success", "", &req)
	}()

	return nil
}
