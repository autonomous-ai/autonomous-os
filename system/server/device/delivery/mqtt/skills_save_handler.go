package mqtthandler

import (
	"encoding/json"
	"errors"
	"log/slog"
	"strings"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/skills"
)

// handleSkillsSave handles kind="skills.save" — the MQTT twin of
// POST /api/agent/skills. Both go through AgentGateway.SaveSkill, so a skill
// pushed from the backend lands in exactly the same place as one authored in the
// web UI's "Write skill" form, in whichever skills dir the ACTIVE runtime owns,
// and honours the same no-overwrite rule.
//
// Unlike skills.install this is SYNCHRONOUS: writing one SKILL.md is a local
// file write measured in milliseconds, so there is nothing to report progress on
// and no reason for a two-phase starting/success dance.
func (h *DeviceMQTTHandler) handleSkillsSave(env domain.MQTTDataCommand) error {
	draft, errMsg := parseSkillsSaveData(env.Data)
	if errMsg != "" {
		return h.publishDataResult(env.Kind, "failure", errMsg, nil)
	}

	// Shares skillsInstallMu with skills.install: both write into the same skills
	// dir, so a role-bundle extract must not interleave with this write. TryLock
	// rather than Lock — an install can take tens of seconds and this runs on the
	// MQTT dispatch path, which must not stall that long.
	if !skillsInstallMu.TryLock() {
		return h.publishDataResult(env.Kind, "failure",
			"a skills install is in progress; try again later", nil)
	}
	defer skillsInstallMu.Unlock()

	runtimeName := h.agentGateway.Name()

	path, err := h.agentGateway.SaveSkill(draft)
	if err != nil {
		step := classifySkillsSaveError(err)
		slog.Error("skills.save: failed", "component", "mqtt",
			"skill", draft.Name, "runtime", runtimeName, "step", step, "error", err)
		return h.publishDataResult(env.Kind, "failure", step+": "+err.Error(), map[string]interface{}{
			"name":        draft.Name,
			"runtime":     runtimeName,
			"failed_step": step,
		})
	}

	slog.Info("skills.save: success", "component", "mqtt",
		"skill", draft.Name, "runtime", runtimeName, "path", path)
	return h.publishDataResult(env.Kind, "success", "", map[string]interface{}{
		"name":    draft.Name,
		"runtime": runtimeName,
		"path":    path,
	})
}

// parseSkillsSaveData decodes + normalises a skills.save payload into the draft
// the gateway takes. Returns a non-empty message when the payload is unusable.
// Kept separate from the handler so the wire contract is testable without a
// broker (publishDataResult needs a live MQTT client).
//
// Only presence is checked here; the NAME SHAPE is left to
// skills.ValidateSkillName inside SaveSkill, so the HTTP and MQTT paths can never
// drift on what a legal skill name is.
func parseSkillsSaveData(raw json.RawMessage) (domain.SkillDraft, string) {
	var req domain.MQTTSkillsSaveData
	if err := json.Unmarshal(raw, &req); err != nil {
		return domain.SkillDraft{}, "invalid skills.save data: " + err.Error()
	}

	draft := domain.SkillDraft{
		Name:         strings.TrimSpace(req.Name),
		Description:  strings.TrimSpace(req.Description),
		Instructions: strings.TrimSpace(req.Instructions),
	}
	if draft.Name == "" || draft.Description == "" || draft.Instructions == "" {
		return domain.SkillDraft{}, "name, description and instructions are required"
	}
	return draft, ""
}

// classifySkillsSaveError maps a SaveSkill failure to the `failed_step` label the
// backend reads off the result, mirroring how skills.install reports its step.
// Anything unrecognised is a plain write failure.
func classifySkillsSaveError(err error) string {
	switch {
	case errors.Is(err, domain.ErrNotSupportedByRuntime):
		return "unsupported_runtime"
	case errors.Is(err, skills.ErrInvalidSkillName):
		return "validate_name"
	case errors.Is(err, skills.ErrSkillExists):
		return "already_exists"
	default:
		return "write"
	}
}
