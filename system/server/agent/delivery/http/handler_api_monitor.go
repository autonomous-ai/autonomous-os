package http

import (
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/runtimes/claudecode"
	"go.autonomous.ai/os/runtimes/codex"
	"go.autonomous.ai/os/runtimes/hermes"
	"go.autonomous.ai/os/runtimes/openclaw"
	"go.autonomous.ai/os/runtimes/opencode"
	"go.autonomous.ai/os/runtimes/picoclaw"
	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/server/serializers"
)

// agentUnitByBackend maps the runtime `Name()` (see domain.AgentRuntime*
// constants) to its systemd unit name. Sourced from each runtime's own
// gateway_unit.go / service_gateway.go — kept in sync with those files.
// Used only by the UI-triggered Restart handler to re-enable the unit
// before restarting; internal restart helpers still call systemctl restart
// directly with the same names.
var agentUnitByBackend = map[string]string{
	domain.AgentRuntimeOpenClaw:   "openclaw",
	domain.AgentRuntimeHermes:     "hermes-gateway",
	domain.AgentRuntimePicoclaw:   "picoclaw",
	domain.AgentRuntimeCodex:      "codex",
	domain.AgentRuntimeClaudeCode: "claudecode",
	domain.AgentRuntimeOpenCode:   "opencode",
}

// GetOpenClawVersion returns the cached OpenClaw binary version (e.g. "2026.5.27").
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

func GetPicoclawVersion() string {
	return picoclaw.GetPicoclawVersion()
}

// populatePicoclawVersion populates the shared picoclaw version cache at startup.
func populatePicoclawVersion() {
	picoclaw.PopulatePicoclawVersion()
}

func GetCodexVersion() string {
	return codex.GetCodexVersion()
}

// populateCodexVersion populates the shared codex version cache at startup.
func populateCodexVersion() {
	codex.PopulateCodexVersion()
}

func GetClaudeCodeVersion() string {
	return claudecode.GetClaudeCodeVersion()
}

// populateClaudeCodeVersion populates the shared claudecode version cache at startup.
func populateClaudeCodeVersion() {
	claudecode.PopulateClaudeCodeVersion()
}

func GetOpenCodeVersion() string {
	return opencode.GetOpenCodeVersion()
}

// populateOpenCodeVersion populates the shared opencode version cache at startup.
func populateOpenCodeVersion() {
	opencode.PopulateOpenCodeVersion()
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

// CancelSpeechHandler silences the turns that are in flight right now and cuts
// whatever the speaker is already playing. Fired by the physical cancel gesture
// (single click) via HAL, so the user gets the floor back without waiting for
// the agent to finish.
//
// Two halves, because neither alone is enough: StopTTS kills the sentence being
// played AND the pre-synthesised queue behind it (hal tts service stop() clears
// the pending list), while the watermark stops os-server from handing HAL the
// sentences that have not been generated yet. Stopping only at HAL means the
// device goes quiet for one sentence and then resumes.
func (h *AgentHandler) CancelSpeechHandler(c *gin.Context) {
	h.CancelSpeech()
	if err := h.agentGateway.StopTTS(); err != nil {
		// The watermark already landed — the backlog stays muted regardless.
		// Report the failure but do not fail the request: a HAL hiccup must
		// not make the gesture look like it did nothing.
		slog.Warn("StopTTS during speech cancel failed", "component", "agent", "error", err)
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

// Restart is the "start + enable + restart" recovery action fired from the
// Overview's Agent Gateway card. It differs from the internal restart callers
// (config refresh, migration) by ALSO re-enabling the unit so the recovery
// survives a reboot.
//
// Steps:
//  1. systemctl enable <unit>  — best-effort; a failed enable does not block
//     the restart (survives reboot is nice-to-have; getting the service running
//     right now is the primary user intent).
//  2. agentGateway.RestartAgent()  — runtime picks the actual command; on
//     openclaw this is `systemctl restart openclaw`, which STARTS the service
//     if it's currently stopped (systemctl restart semantics), so an operator
//     who stopped+disabled a broken gateway can recover from the web UI without
//     SSH.
func (h *AgentHandler) Restart(c *gin.Context) {
	name := h.agentGateway.Name()
	slog.Info("agent restart requested", "component", "agent", "backend", name)

	enabled := false
	if unit, ok := agentUnitByBackend[name]; ok && unit != "" && os.Geteuid() == 0 {
		if _, err := exec.LookPath("systemctl"); err == nil {
			if out, err := exec.Command("systemctl", "enable", unit).CombinedOutput(); err != nil {
				slog.Warn("systemctl enable failed (best-effort, continuing to restart)",
					"component", "agent", "backend", name, "unit", unit,
					"error", err, "output", strings.TrimSpace(string(out)))
			} else {
				enabled = true
				slog.Info("systemctl enabled for auto-start on boot",
					"component", "agent", "backend", name, "unit", unit)
			}
		}
	}

	if err := h.agentGateway.RestartAgent(); err != nil {
		slog.Warn("RestartAgent failed", "component", "agent", "backend", name, "error", err)
		c.JSON(http.StatusBadGateway, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"backend": name,
		"enabled": enabled,
	}))
}

// Status returns the current agent connection status.
func (h *AgentHandler) Status(c *gin.Context) {
	// Get real emotion from HAL (source of truth) instead of parsed text
	emotion := h.fetchHALEmotion()

	// Active backend's own version (OpenClaw → "2026.5.27", Hermes → "0.17.0"),
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

// ConfigJSON returns the active runtime's raw config contents for the gw-config UI.
func (h *AgentHandler) ConfigJSON(c *gin.Context) {
	data, err := h.agentGateway.GetConfigJSON()
	if err != nil {
		if errors.Is(err, domain.ErrNotSupportedByRuntime) {
			c.JSON(http.StatusOK, serializers.ResponseError(
				h.agentGateway.Name()+" has no device-side config file"))
			return
		}
		c.JSON(http.StatusOK, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(data))
}
