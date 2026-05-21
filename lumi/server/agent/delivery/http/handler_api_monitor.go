package http

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"regexp"
	"strings"
	"sync/atomic"
	"time"

	"github.com/gin-gonic/gin"

	"go-lamp.autonomous.ai/lib/core/system"
	"go-lamp.autonomous.ai/lib/lelamp"
	"go-lamp.autonomous.ai/server/serializers"
)

// openclawSemverRe captures the first semver-like token in `openclaw --version`
// output (e.g. "OpenClaw 2026.3.8 (3caab92)" → "2026.3.8"). Mirrors the regex
// used in bootstrap; duplicated here to avoid pulling bootstrap into server.
var openclawSemverRe = regexp.MustCompile(`(\d+\.\d+\.\d+(?:[-+._][0-9A-Za-z.-]+)?)`)

// openClawVersion caches the OpenClaw runtime version. Package-level (not
// a struct field) because AgentHandler is returned by value through wire
// and a struct copy would orphan the field. Populated once at handler init
// via populateOpenClawVersion(); stays valid until the process restarts.
var openClawVersion atomic.Pointer[string]

// GetOpenClawVersion returns the cached OpenClaw binary version (e.g. "2026.5.7").
// Empty string if openclaw is not installed or version has not been populated yet.
func GetOpenClawVersion() string {
	if v := openClawVersion.Load(); v != nil {
		return *v
	}
	return ""
}

// populateOpenClawVersion shells out to `openclaw --version` with a short
// timeout and stores the normalized semver in openClawVersion. Empty result
// when openclaw is not on PATH or the command fails — the Status endpoint
// then returns "" and the UI renders nothing for that field.
func populateOpenClawVersion() {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	out, err := system.Run(ctx, "openclaw", "--version")
	if err != nil {
		slog.Warn("read openclaw version failed", "component", "openclaw", "error", err)
		return
	}
	line := strings.TrimSpace(strings.TrimRight(string(out), "\r\n"))
	if i := strings.IndexByte(line, '\n'); i >= 0 {
		line = strings.TrimSpace(line[:i])
	}
	v := ""
	if loc := openclawSemverRe.FindStringSubmatch(line); len(loc) > 1 {
		v = loc[1]
	}
	openClawVersion.Store(&v)
}

// StopTTS interrupts active TTS playback on LeLamp.
func (h *AgentHandler) StopTTS(c *gin.Context) {
	if err := h.agentGateway.StopTTS(); err != nil {
		slog.Warn("StopTTS failed", "component", "openclaw", "error", err)
		c.JSON(http.StatusBadGateway, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(nil))
}

// SetBusy marks the agent as busy from an external signal (e.g. turn-gate hook firing at
// message:preprocessed before lifecycle_start SSE arrives). Closes the timing gap for
// channel-initiated turns (Telegram, Slack, Discord) that bypass Lumi server entirely.
func (h *AgentHandler) SetBusy(c *gin.Context) {
	h.agentGateway.SetBusy(true)
	c.JSON(http.StatusOK, serializers.ResponseSuccess(nil))
}

// Status returns the current agent connection status.
func (h *AgentHandler) Status(c *gin.Context) {
	// Get real emotion from LeLamp (source of truth) instead of parsed text
	emotion := h.fetchLeLampEmotion()

	version := ""
	if v := openClawVersion.Load(); v != nil {
		version = *v
	}

	// uptime: seconds since the WS connection last became ready (resets when
	// Lumi reconnects). agentUptime: actual OpenClaw process uptime sourced from
	// the gateway's hello-ok payload — survives Lumi restarts. The UI shows
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

// fetchLeLampEmotion calls LeLamp /emotion/status to get the current emotion.
// Falls back to lastEmotion if LeLamp is unreachable.
func (h *AgentHandler) fetchLeLampEmotion() string {
	emotion, err := lelamp.GetEmotion()
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
