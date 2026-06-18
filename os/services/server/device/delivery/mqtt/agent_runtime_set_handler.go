package mqtthandler

import (
	"encoding/json"
	"log/slog"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/internal/device"
)

// handleAgentRuntimeSet applies an `agent_runtime.set` downlink — swap the
// agentic backend (openclaw ⇄ hermes). Same shape as realtime.set: ack
// immediately, apply async, then ack the outcome. The apply persists
// config.agent_runtime and spawns switch-runtime.sh, which toggles the systemd
// units (start target / stop the other) and restarts os-server so
// agent/factory.go re-resolves the gateway. Because os-server restarts itself,
// the "success" ack is published BEFORE the restart fires — the BFF should
// expect a short reconnect after it.

func (h *DeviceMQTTHandler) publishAgentRuntimeSetAck(status, errMsg string, data *domain.AgentRuntimeSetData) {
	ack := domain.AgentRuntimeSetAck{
		MQTTInfoResponse: domain.NewMQTTInfoResponse(h.config, "data", device.GetDeviceMac()),
		Kind:             domain.KindAgentRuntimeSet,
		Status:           status,
		Error:            errMsg,
		Data:             data,
	}
	if err := h.publish(ack); err != nil {
		slog.Warn("agent_runtime.set: publish ack failed", "component", "mqtt", "status", status, "error", err)
	}
}

func (h *DeviceMQTTHandler) handleAgentRuntimeSet(env domain.MQTTDataCommand) error {
	var req domain.AgentRuntimeSetData
	if err := json.Unmarshal(env.Data, &req); err != nil {
		slog.Error("agent_runtime.set: invalid payload", "component", "mqtt", "error", err)
		h.publishAgentRuntimeSetAck("failure", "invalid JSON payload", nil)
		return err
	}

	slog.Info("agent_runtime.set: received", "component", "mqtt", "runtime", req.Runtime)

	// Ack immediately so BFF knows the device received the command.
	h.publishAgentRuntimeSetAck("starting", "", nil)

	go func() {
		if err := h.deviceService.UpdateAgentRuntime(req); err != nil {
			slog.Error("agent_runtime.set: UpdateAgentRuntime failed", "component", "mqtt", "error", err)
			h.publishAgentRuntimeSetAck("failure", err.Error(), &req)
			return
		}
		// UpdateAgentRuntime saved config + spawned switch-runtime.sh, which will
		// restart os-server shortly. ACK success NOW, before the restart kills us
		// — the BFF confirms the swap from the next AGENT BACKEND ACTIVE banner.
		slog.Info("agent_runtime.set: applied — switch script spawned, os-server restart imminent",
			"component", "mqtt", "runtime", req.Runtime)
		h.publishAgentRuntimeSetAck("success", "", &req)
	}()

	return nil
}
