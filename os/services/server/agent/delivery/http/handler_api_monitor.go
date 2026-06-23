package http

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/internal/device"
	"go.autonomous.ai/os/internal/hermes"
	"go.autonomous.ai/os/internal/openclaw"
	"go.autonomous.ai/os/lib/hal"
	"go.autonomous.ai/os/server/serializers"
)

// GetOpenClawVersion returns the cached OpenClaw binary version (e.g. "2026.6.9").
// The cache lives in the openclaw package — single source of truth, shared with
// the MQTT `info` message and the channel-config writers — so this is a thin
// pass-through for the agent HTTP/MQTT handlers.
func GetOpenClawVersion() string {
	return openclaw.GetOpenClawVersion()
}

// populateOpenClawVersion populates the shared openclaw version cache at startup.
func populateOpenClawVersion() {
	openclaw.PopulateOpenClawVersion()
}

// GetHermesVersion returns the cached Hermes CLI version (e.g. "0.17.0"). Thin
// pass-through to the hermes package cache, mirroring GetOpenClawVersion so the
// MQTT `info` message can report hermes_version next to openclaw_version.
func GetHermesVersion() string {
	return hermes.GetHermesVersion()
}

// populateHermesVersion populates the shared hermes version cache at startup.
func populateHermesVersion() {
	hermes.PopulateHermesVersion()
}

// StopTTS interrupts active TTS playback on HAL.
func (h *AgentHandler) StopTTS(c *gin.Context) {
	if err := h.agentGateway.StopTTS(); err != nil {
		slog.Warn("StopTTS failed", "component", "agent", "backend", h.agentGateway.Name(), "error", err)
		c.JSON(http.StatusBadGateway, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(nil))
}

// SetBusy marks the agent as busy from an external signal (e.g. turn-gate hook firing at
// message:preprocessed before lifecycle_start SSE arrives). Closes the timing gap for
// channel-initiated turns (Telegram, Slack, Discord) that bypass the OS server entirely.
func (h *AgentHandler) SetBusy(c *gin.Context) {
	h.agentGateway.SetBusy(true)
	c.JSON(http.StatusOK, serializers.ResponseSuccess(nil))
}

// Status returns the current agent connection status.
func (h *AgentHandler) Status(c *gin.Context) {
	// Get real emotion from HAL (source of truth) instead of parsed text
	emotion := h.fetchHALEmotion()

	// Active backend's own version (OpenClaw → "2026.6.9", Hermes → "0.17.0"),
	// so the web Overview shows the running runtime's version, not always OpenClaw's.
	version := h.agentGateway.Version()

	// uptime: seconds since the WS connection last became ready (resets when
	// the OS server reconnects). agentUptime: actual OpenClaw process uptime sourced from
	// the gateway's hello-ok payload — survives OS server restarts. The UI shows
	// agentUptime; uptime stays for debugging WS reconnect cadence.
	var uptime int64
	if connectedAt := h.agentGateway.ConnectedAt(); connectedAt > 0 {
		uptime = time.Now().Unix() - connectedAt
		if uptime < 0 {
			uptime = 0
		}
	}

	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"name":        h.agentGateway.Name(),
		"connected":   h.agentGateway.IsReady(),
		"sessionKey":  h.agentGateway.GetSessionKey() != "",
		"emotion":     emotion,
		"version":     version,
		"uptime":      uptime,
		"agentUptime": h.agentGateway.AgentUptime(),
	}))
}

// fetchHALEmotion calls HAL /emotion/status to get the current emotion.
// Falls back to lastEmotion if HAL is unreachable.
func (h *AgentHandler) fetchHALEmotion() string {
	// Only devices that declare the `expression` capability mount HAL's /emotion
	// route. On a device without it (e.g. intern-v2: audio+light only) the call
	// just 404s on every status poll. Gate on the declared capability so the OS
	// never reaches for a route the body doesn't have.
	if !device.Has(h.config.DeviceTypeOrDefault(), device.CapExpression) {
		return ""
	}
	emotion, err := hal.GetEmotion()
	if err != nil {
		h.lastEmotionMu.Lock()
		defer h.lastEmotionMu.Unlock()
		return h.lastEmotion
	}
	return emotion
}

// Events streams monitor bus events over SSE to connected web UI clients.
func (h *AgentHandler) Events(c *gin.Context) {
	c.Header("Content-Type", "text/event-stream")
	c.Header("Cache-Control", "no-cache")
	c.Header("Connection", "keep-alive")
	c.Header("X-Accel-Buffering", "no") // disable nginx buffering

	sub, unsub := h.monitorBus.Subscribe()
	defer unsub()

	c.Stream(func(w io.Writer) bool {
		select {
		case evt := <-sub:
			data, _ := json.Marshal(evt)
			c.SSEvent("message", string(data))
			return true
		case <-c.Request.Context().Done():
			return false
		}
	})
}

// ConfigJSON returns the raw openclaw.json contents for the gw-config UI.
func (h *AgentHandler) ConfigJSON(c *gin.Context) {
	data, err := h.agentGateway.GetConfigJSON()
	if err != nil {
		c.JSON(http.StatusOK, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(data))
}
