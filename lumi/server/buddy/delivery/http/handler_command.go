package http

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
	"go-lamp.autonomous.ai/internal/buddy"
	"go-lamp.autonomous.ai/server/serializers"
)

type commandRequest struct {
	ID        string         `json:"id"`
	Action    string         `json:"action" binding:"required"`
	Params    map[string]any `json:"params"`
	TimeoutMs int            `json:"timeout_ms"`
}

// Command dispatches one command to the connected buddy and returns the buddy's
// response. Localhost-only at the route layer (OpenClaw skill on the lamp is
// the intended caller).
func (h *BuddyHandler) Command(c *gin.Context) {
	var req commandRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if req.Params == nil {
		req.Params = map[string]any{}
	}
	cmd := buddy.Command{
		ID:        req.ID,
		Action:    req.Action,
		Params:    req.Params,
		TimeoutMs: req.TimeoutMs,
		IssuedAt:  time.Now().UTC().Format(time.RFC3339),
		IssuedBy:  "api:/api/buddy/command",
	}
	if cmd.ID == "" {
		cmd.ID = buddy.NewCommandID()
	}
	slog.Info("buddy /command request", "component", "buddy", "id", cmd.ID, "action", cmd.Action, "param_keys", mapKeys(cmd.Params), "connected", h.service.Connected())

	timeout := 30 * time.Second
	if req.TimeoutMs > 0 {
		timeout = time.Duration(req.TimeoutMs)*time.Millisecond + 5*time.Second
	}
	ctx, cancel := context.WithTimeout(c.Request.Context(), timeout)
	defer cancel()

	raw, err := h.service.Dispatch(ctx, cmd)
	if err != nil {
		slog.Warn("buddy /command dispatch error", "component", "buddy", "id", cmd.ID, "action", cmd.Action, "error", err)
		c.JSON(http.StatusBadGateway, serializers.ResponseError(err.Error()))
		return
	}
	slog.Info("buddy /command response", "component", "buddy", "id", cmd.ID, "action", cmd.Action, "bytes", len(raw))
	// Buddy's response is already shaped {id, ok, result, error, duration_ms}.
	// Pass it through inside the lumi envelope so callers get a consistent
	// {status: 1, data: <buddy-response>, message: null}.
	var inner map[string]any
	if err := json.Unmarshal(raw, &inner); err != nil {
		c.JSON(http.StatusOK, serializers.ResponseSuccess(json.RawMessage(raw)))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(inner))
}

// Exec is the marker-friendly entry point used by OpenClaw skills via the
// `[HW:/buddy/exec/<action>:{...}]` inline marker. URL path carries the action;
// JSON body is the params blob. This sidesteps the HW-marker regex limitation
// (no nested `{}` allowed in body) by keeping params flat per call.
//
// For richer use (vision loop, multi-step) the OpenClaw skill should call
// /api/buddy/command directly with the full Command schema.
func (h *BuddyHandler) Exec(c *gin.Context) {
	action := c.Param("action")
	if action == "" {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("missing action"))
		return
	}
	body, _ := io.ReadAll(c.Request.Body)
	params := map[string]any{}
	if len(body) > 0 {
		if err := json.Unmarshal(body, &params); err != nil {
			c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid params json: "+err.Error()))
			return
		}
	}
	cmd := buddy.Command{
		ID:        buddy.NewCommandID(),
		Action:    action,
		Params:    params,
		TimeoutMs: 10000,
		IssuedAt:  time.Now().UTC().Format(time.RFC3339),
		IssuedBy:  "skill:hw-marker",
	}
	slog.Info("buddy /exec request", "component", "buddy", "id", cmd.ID, "action", action, "param_keys", mapKeys(params), "connected", h.service.Connected())
	ctx, cancel := context.WithTimeout(c.Request.Context(), 15*time.Second)
	defer cancel()

	raw, err := h.service.Dispatch(ctx, cmd)
	if err != nil {
		slog.Warn("buddy /exec dispatch error", "component", "buddy", "id", cmd.ID, "action", action, "error", err)
		c.JSON(http.StatusBadGateway, serializers.ResponseError(err.Error()))
		return
	}
	slog.Info("buddy /exec response", "component", "buddy", "id", cmd.ID, "action", action, "bytes", len(raw))
	var inner map[string]any
	if err := json.Unmarshal(raw, &inner); err != nil {
		c.JSON(http.StatusOK, serializers.ResponseSuccess(json.RawMessage(raw)))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(inner))
}

// mapKeys returns the sorted keys of a params map for log fields. Values are
// intentionally omitted — a `type_text` body can be sensitive (passwords) and a
// screenshot response carries multi-KB base64. The action + key list is enough
// to confirm "which command, with which fields" without spamming logs.
func mapKeys(m map[string]any) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	return keys
}
