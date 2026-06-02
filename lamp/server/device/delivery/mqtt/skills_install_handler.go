package mqtthandler

import (
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"

	"go-lamp.autonomous.ai/domain"
	"go-lamp.autonomous.ai/internal/openclaw"
)

// skillsInstallMu serializes role-skill installs: concurrent extracts into the
// same skills dir would race on shared subpaths.
var skillsInstallMu sync.Mutex

// handleSkillsInstall handles kind="skills.install". Acks "starting", then
// downloads the role's skills.zip and extracts it into the openclaw skills dir
// asynchronously. Cumulative (other roles' skills preserved) and no gateway
// restart (skills.load.watch picks new files up per session).
func (h *DeviceMQTTHandler) handleSkillsInstall(env domain.MQTTDataCommand) error {
	var req domain.MQTTSkillsInstallData
	if err := json.Unmarshal(env.Data, &req); err != nil {
		return h.publishDataResult(env.Kind, "failure", "invalid skills.install data: "+err.Error(), nil)
	}
	req.Role = strings.TrimSpace(req.Role)
	if req.Role == "" {
		return h.publishDataResult(env.Kind, "failure", "role is required", nil)
	}

	if !skillsInstallMu.TryLock() {
		return h.publishDataResult(env.Kind, "failure",
			"another skills install is already in progress; try again later", nil)
	}

	if err := h.publishDataResult(env.Kind, "starting", "", nil); err != nil {
		slog.Error("skills.install: ack publish failed", "component", "mqtt", "error", err)
	}

	go func() {
		defer skillsInstallMu.Unlock()
		h.runSkillsInstall(env.Kind, req.Role)
	}()
	return nil
}

// runSkillsInstall downloads + extracts the role skills and publishes the
// terminal status.
func (h *DeviceMQTTHandler) runSkillsInstall(kind, role string) {
	slog.Info("skills.install: start", "component", "mqtt", "role", role)

	count, err := openclaw.InstallRoleSkills(h.config.OpenclawConfigDir, role)
	if err != nil {
		step := "install"
		if errors.Is(err, openclaw.ErrInvalidRole) {
			step = "validate_role"
		}
		slog.Error("skills.install: failed", "component", "mqtt", "role", role, "step", step, "error", err)
		_ = h.publishDataResult(kind, "failure", fmt.Sprintf("%s: %s", step, err.Error()), map[string]interface{}{
			"role":        role,
			"failed_step": step,
		})
		return
	}

	slog.Info("skills.install: success", "component", "mqtt", "role", role, "files", count)
	_ = h.publishDataResult(kind, "success", "", map[string]interface{}{
		"role":          role,
		"files_written": count,
	})
}
