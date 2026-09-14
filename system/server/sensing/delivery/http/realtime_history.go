package http

import (
	"log/slog"
	"net/http"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/server/serializers"
	"go.autonomous.ai/os/system/telemetry"
)

// SetRealtimeHistory replaces the volatile sensing queue for completed realtime
// exchanges. Speaker supersession still runs before this persistence hook.
func (h *SensingHandler) SetRealtimeHistory(fn func(string, string) (string, error)) {
	h.realtimeHistory = fn
}

func (h *SensingHandler) persistRealtimeHistory(c *gin.Context, req SensingEventRequest, suppressed bool) {
	runID, err := h.realtimeHistory(req.InteractionID, reSnapshotPath.ReplaceAllString(req.Message, ""))
	if err != nil {
		slog.Error("persist realtime history", "error", err)
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("Could not save realtime conversation history"))
		return
	}
	telemetry.ReportTaskStarted(req.Type, req.InteractionID, runID)
	// Preserve the original sensing evidence and look thumbnail for the web turn.
	start := flow.Start("sensing_input", map[string]any{
		"type": req.Type, "message": req.Message, "interaction_id": req.InteractionID,
	}, runID)
	flow.End("sensing_input", start, map[string]any{"path": "external_history", "queued": true}, runID)
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"handler": "external_history", "runId": runID, "speechSuppressed": suppressed,
	}))
}
