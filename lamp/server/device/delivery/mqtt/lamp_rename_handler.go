package mqtthandler

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"

	"go-lamp.autonomous.ai/domain"
	"go-lamp.autonomous.ai/lib/i18n"
)

// handleLampRename rewrites the agent name in workspace/IDENTITY.md. WatchIdentity
// will pick up the change on its next poll cycle and push fresh wake words to
// LeLamp; OpenClaw re-reads IDENTITY.md on its own so no gateway restart is needed.
// After the file write succeeds, an async system chat message tells the agent its
// new name so it greets the owner with the new identity in the next turn.
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

	// Tell the agent its new name asynchronously — failures don't fail the ack.
	// IDENTITY.md was already written above; this system message gives the agent
	// the new identity inline so the very next reply uses it without waiting for
	// OpenClaw to re-read the file.
	go func() {
		prompt := renameGreetingPrompt(name)
		if _, err := h.agentGateway.SendSystemChatMessage(prompt); err != nil {
			slog.Warn("lamp.rename: notify agent failed", "component", "mqtt", "error", err)
		}
	}()

	slog.Info("lamp.rename: applied", "component", "mqtt", "name", name)
	return h.publishDataResult(domain.KindLampRename, "success", "", map[string]interface{}{
		"name": name,
	})
}

// renameGreetingPrompt builds the [system] message that tells the agent it was
// just renamed. Phrased in the owner's current STT language (read from i18n at
// call time) so the very first acknowledgment reply lands in that language —
// SOUL.md's "mirror owner's turn language" only kicks in once owner speaks.
func renameGreetingPrompt(name string) string {
	switch i18n.Lang() {
	case i18n.LangVI:
		return fmt.Sprintf("[system] Chủ nhân vừa đổi tên cho bạn thành \"%s\". Chào lại ngắn gọn, ấm áp.", name)
	case i18n.LangZhCN:
		return fmt.Sprintf("[system] 主人刚刚把你的名字改成了 \"%s\"，请简短温暖地回应。", name)
	case i18n.LangZhTW:
		return fmt.Sprintf("[system] 主人剛剛把你的名字改成了 \"%s\"，請簡短溫暖地回應。", name)
	}
	return fmt.Sprintf("[system] Your owner just renamed you to \"%s\". Acknowledge briefly and warmly.", name)
}
