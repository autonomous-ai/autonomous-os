package http

import (
	"log/slog"
	"net/http"

	"github.com/gin-gonic/gin"

	migratepersona "go.autonomous.ai/os/system/agent/migrate_persona"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/server/serializers"
)

// ResetMemory handles POST /api/agent/memory/reset — the no-SSH recovery for a
// self-poisoned agent memory (#421). Backs up then clears USER.md / MEMORY.md /
// KNOWLEDGE.md and the realtime summary in every runtime workspace, then
// re-runs onboarding so KNOWLEDGE.md is re-seeded from its template right away
// instead of on the next boot. Files only — the in-flight session is untouched;
// the caller follows with /new if the poison is also in session history.
func (h *AgentHandler) ResetMemory(c *gin.Context) {
	opts := migratepersona.DefaultOptions(h.config.OpenclawConfigDir, "")
	rep, err := migratepersona.ResetMemoryFiles(opts)
	if err != nil {
		// rep carries the partial progress (what was already backed up and
		// cleared before the failure) so the operator can find the backups.
		slog.Error("memory reset failed", "component", "memory-guard", "error", err, "cleared", rep.Cleared, "backup_dirs", rep.BackupDirs)
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("memory reset: "+err.Error()))
		return
	}
	// Counts and paths only — never memory text — in the flow event.
	flow.Log("memory_reset", map[string]any{"cleared": len(rep.Cleared), "backup_dirs": rep.BackupDirs})
	slog.Warn("agent memory reset", "component", "memory-guard", "cleared", len(rep.Cleared), "backups", rep.BackupDirs)
	if err := h.agentGateway.EnsureOnboarding(); err != nil {
		// Not fatal: the reset itself succeeded; KNOWLEDGE.md is re-seeded on
		// the next boot pass instead.
		slog.Warn("onboarding re-seed after memory reset failed", "component", "memory-guard", "error", err)
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(rep))
}
