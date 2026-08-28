package mqtthandler

import (
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/schedule"
	"go.autonomous.ai/os/system/server/serializers"
)

// scheduleWriteRequest is the JSON body of the device-side create/update
// endpoints. Only the user-editable subset — id, rev, status and every run
// bookkeeping field are backend-owned and have no representation here, so the
// local UI cannot propose them any more than the wire payload can.
//
// Enabled is a POINTER so "not supplied" is distinguishable from "false" on a
// PATCH: without that, editing only a task's name would silently pause it.
type scheduleWriteRequest struct {
	Name         string        `json:"name"`
	Instructions string        `json:"instructions"`
	Enabled      *bool         `json:"enabled,omitempty"`
	TemplateCode string        `json:"template_code,omitempty"`
	Cadence      schedule.Spec `json:"schedule"`
	EndAt        *time.Time    `json:"end_at,omitempty"`
}

// queueIntent validates, persists and immediately tries to publish one intent.
//
// The publish is best-effort ON PURPOSE and its failure is not reported to the
// caller: the intent is already durably queued, so an offline device has
// genuinely accepted the user's edit and will deliver it on reconnect.
// Returning an error here would tell the user their change was lost when it
// was not.
func (h *DeviceMQTTHandler) queueIntent(in schedule.Intent) error {
	if err := h.scheduleIntents.Append(in); err != nil {
		return err
	}
	h.publishIntent(in)
	return nil
}

// CreateSchedule handles POST /api/schedule.
//
// Note what this does NOT do: it does not write to schedules.json. A task the
// backend has not confirmed never enters the file the runner reads, so it
// cannot fire early. The UI still shows it immediately, because ListSchedules
// overlays the pending queue — visible, but not yet armed.
func (h *DeviceMQTTHandler) CreateSchedule(c *gin.Context) {
	var req scheduleWriteRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid body: "+err.Error()))
		return
	}

	enabled := true
	if req.Enabled != nil {
		enabled = *req.Enabled
	}
	payload := &schedule.IntentPayload{
		Name:         strings.TrimSpace(req.Name),
		Instructions: strings.TrimSpace(req.Instructions),
		Enabled:      enabled,
		TemplateCode: req.TemplateCode,
		Cadence:      req.Cadence,
		EndAt:        req.EndAt,
		Timezone:     h.scheduleStore.Timezone().String(),
	}
	if err := schedule.ValidateIntentPayload(payload); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}

	intentID, err := schedule.NewIntentID()
	if err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}
	in := schedule.Intent{
		IntentID:  intentID,
		Op:        "create",
		Payload:   payload,
		CreatedAt: time.Now(),
	}
	if err := h.queueIntent(in); err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}

	c.JSON(http.StatusAccepted, serializers.ResponseSuccess(map[string]any{
		"intent_id": intentID,
		"pending":   "create",
		"schedule":  payload,
	}))
}

// UpdateSchedule handles PATCH /api/schedule/:id.
//
// The stored row's rev is quoted as base_rev — this is the compare half of the
// backend's compare-and-swap, and the reason an edit made on a device that has
// been offline cannot silently overwrite a newer edit made in the app.
func (h *DeviceMQTTHandler) UpdateSchedule(c *gin.Context) {
	id := strings.TrimSpace(c.Param("id"))
	current, ok := h.scheduleStore.Get(id)
	if !ok {
		c.JSON(http.StatusNotFound, serializers.ResponseError("unknown schedule id: "+id))
		return
	}

	var req scheduleWriteRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid body: "+err.Error()))
		return
	}

	// Start from what is stored and layer the supplied fields on top, so a
	// PATCH that names only one field leaves the rest of the task alone.
	payload := &schedule.IntentPayload{
		Name:         current.Name,
		Instructions: current.Instructions,
		Enabled:      current.Enabled,
		Cadence:      current.Cadence,
		EndAt:        current.EndAt,
		Timezone:     h.scheduleStore.Timezone().String(),
	}
	if s := strings.TrimSpace(req.Name); s != "" {
		payload.Name = s
	}
	if s := strings.TrimSpace(req.Instructions); s != "" {
		payload.Instructions = s
	}
	if req.Enabled != nil {
		payload.Enabled = *req.Enabled
	}
	if req.Cadence.Repeat != "" {
		payload.Cadence = req.Cadence
	}
	if req.EndAt != nil {
		payload.EndAt = req.EndAt
	}
	if err := schedule.ValidateIntentPayload(payload); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}

	intentID, err := schedule.NewIntentID()
	if err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}
	in := schedule.Intent{
		IntentID:   intentID,
		Op:         "update",
		ScheduleID: id,
		BaseRev:    current.Rev,
		Payload:    payload,
		CreatedAt:  time.Now(),
	}
	if err := h.queueIntent(in); err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}

	c.JSON(http.StatusAccepted, serializers.ResponseSuccess(map[string]any{
		"intent_id": intentID,
		"pending":   "update",
		"id":        id,
		"base_rev":  current.Rev,
		"schedule":  payload,
	}))
}

// DeleteSchedule handles DELETE /api/schedule/:id. Like the other two writes
// this only queues a proposal — the row stays in schedules.json, and therefore
// stays runnable, until the backend confirms the delete and the resulting
// schedule.sync removes it. That is deliberate: a delete that the backend
// rejects must not have already stopped the task.
func (h *DeviceMQTTHandler) DeleteSchedule(c *gin.Context) {
	id := strings.TrimSpace(c.Param("id"))
	current, ok := h.scheduleStore.Get(id)
	if !ok {
		c.JSON(http.StatusNotFound, serializers.ResponseError("unknown schedule id: "+id))
		return
	}

	intentID, err := schedule.NewIntentID()
	if err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}
	in := schedule.Intent{
		IntentID:   intentID,
		Op:         "delete",
		ScheduleID: id,
		BaseRev:    current.Rev,
		CreatedAt:  time.Now(),
	}
	if err := h.queueIntent(in); err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}

	c.JSON(http.StatusAccepted, serializers.ResponseSuccess(map[string]any{
		"intent_id": intentID,
		"pending":   "delete",
		"id":        id,
		"base_rev":  current.Rev,
	}))
}
