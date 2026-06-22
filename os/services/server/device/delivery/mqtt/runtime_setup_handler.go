package mqtthandler

import (
	"log/slog"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/internal/device"
)

// handleRuntimeSetup applies a `hermes.setup` / `picoclaw.setup` downlink — swap
// the active agentic backend. The kind itself names the target runtime (passed
// in by the dispatcher), so unlike the former generic agent_runtime.set there is
// no runtime field to read off the wire. Same shape as realtime.set: ack
// immediately, apply async, then ack the outcome. The apply persists
// config.agent_runtime and spawns switch-runtime.sh, which toggles the systemd
// units (start target / stop the other) and restarts os-server so
// agent/factory.go re-resolves the gateway. Because os-server restarts itself,
// the "success" ack is published BEFORE the restart fires — the worker should
// expect a short reconnect after it. Every ack echoes the triggering kind so the
// worker can match hermes.setup vs picoclaw.setup.

func (h *DeviceMQTTHandler) publishRuntimeSetupAck(kind, status, errMsg string, data *domain.AgentRuntimeSetData) {
	ack := domain.AgentRuntimeSetAck{
		MQTTInfoResponse: domain.NewMQTTInfoResponse(h.config, "data", device.GetDeviceMac()),
		Kind:             kind,
		Status:           status,
		Error:            errMsg,
		Data:             data,
	}
	if err := h.publish(ack); err != nil {
		slog.Warn("runtime setup: publish ack failed", "component", "mqtt", "kind", kind, "status", status, "error", err)
	}
}

// handleRuntimeSetup is shared by the hermes.setup and picoclaw.setup dispatch
// cases; runtime is the target backend named by the kind.
func (h *DeviceMQTTHandler) handleRuntimeSetup(env domain.MQTTDataCommand, runtime string) error {
	kind := env.Kind
	req := domain.AgentRuntimeSetData{Runtime: runtime}

	slog.Info("runtime setup: received", "component", "mqtt", "kind", kind, "runtime", runtime)

	// Ack immediately so the worker knows the device received the command.
	h.publishRuntimeSetupAck(kind, "starting", "", nil)

	go func() {
		if err := h.deviceService.UpdateAgentRuntime(req); err != nil {
			slog.Error("runtime setup: UpdateAgentRuntime failed", "component", "mqtt", "kind", kind, "error", err)
			h.publishRuntimeSetupAck(kind, "failure", err.Error(), &req)
			return
		}
		// UpdateAgentRuntime saved config + spawned switch-runtime.sh, which will
		// restart os-server shortly. ACK success NOW, before the restart kills us
		// — the worker confirms the swap from the next AGENT BACKEND ACTIVE banner.
		slog.Info("runtime setup: applied — switch script spawned, os-server restart imminent",
			"component", "mqtt", "kind", kind, "runtime", runtime)
		h.publishRuntimeSetupAck(kind, "success", "", &req)
	}()

	return nil
}
