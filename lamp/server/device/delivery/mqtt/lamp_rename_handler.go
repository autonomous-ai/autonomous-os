package mqtthandler

import (
	"encoding/json"
	"log/slog"
	"strings"

	"go-lamp.autonomous.ai/domain"
)

// handleLampRename rewrites the agent name in workspace/IDENTITY.md. WatchIdentity
// will pick up the change on its next poll cycle and push fresh wake words to
// LeLamp; OpenClaw re-reads IDENTITY.md on its own so no gateway restart is needed.
func (h *DeviceMQTTHandler) handleLampRename(env domain.MQTTDataCommand) error {
	var req domain.MQTTLampRenameData
	if err := json.Unmarshal(env.Data, &req); err != nil {
		slog.Error("lamp.rename: invalid payload", "component", "mqtt", "error", err)
		return h.publishDataResult(domain.KindLampRename, "failure", "invalid JSON payload", nil)
	}

	name := strings.TrimSpace(req.Name)
	if name == "" {
		slog.Warn("lamp.rename: missing name", "component", "mqtt")
		return h.publishDataResult(domain.KindLampRename, "failure", "name is required", nil)
	}

	slog.Info("lamp.rename: received", "component", "mqtt", "name", name)

	if err := h.agentGateway.UpdateIdentityName(name); err != nil {
		slog.Error("lamp.rename: UpdateIdentityName failed", "component", "mqtt", "error", err)
		return h.publishDataResult(domain.KindLampRename, "failure", err.Error(), nil)
	}

	slog.Info("lamp.rename: applied", "component", "mqtt", "name", name)
	return h.publishDataResult(domain.KindLampRename, "success", "", map[string]interface{}{
		"name": name,
	})
}
