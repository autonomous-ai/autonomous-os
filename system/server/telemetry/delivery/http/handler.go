// Package http exposes the device-local ingestion endpoint for tracking
// events produced outside os-server — HAL today, any other on-device process
// later. It does nothing but validate and hand over to system/tracking: the
// measurement lives with whoever can observe it, this is only the pipe.
package http

import (
	"log/slog"
	"net/http"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/server/serializers"
	"go.autonomous.ai/os/system/tracking"
)

// maxParams bounds one event. Generous for a KPI record, small enough that a
// looping producer cannot post a payload worth forwarding to AA.
const maxParams = 64

// EventRequest is the wire shape of POST /api/tracking/event.
type EventRequest struct {
	EventName string         `json:"event_name" binding:"required"`
	EventID   string         `json:"event_id"`
	Params    map[string]any `json:"params"`
}

// TrackingHandler serves the ingestion endpoint. Stateless — the queue and
// the de-duplication live in system/tracking.
type TrackingHandler struct{}

func ProvideTrackingHandler() *TrackingHandler { return &TrackingHandler{} }

// PostEvent accepts one event and queues it. Always 200 on a well-formed
// body: the producer is on the voice path and must never be made to retry or
// wait on the warehouse being reachable.
func (h *TrackingHandler) PostEvent(c *gin.Context) {
	var req EventRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid tracking event: "+err.Error()))
		return
	}
	if len(req.Params) > maxParams {
		slog.Warn("[tracking] event rejected -- too many params", "component", "tracking",
			"event_name", req.EventName, "params", len(req.Params))
		c.JSON(http.StatusBadRequest, serializers.ResponseError("too many params"))
		return
	}

	tracking.Report(tracking.Event{
		Name:   req.EventName,
		ID:     req.EventID,
		Params: req.Params,
	})
	c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"accepted": true}))
}
