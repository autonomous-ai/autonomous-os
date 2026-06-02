package http

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync/atomic"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/go-playground/validator/v10"

	"go-lamp.autonomous.ai/domain"
	"go-lamp.autonomous.ai/internal/intent"
	"go-lamp.autonomous.ai/internal/monitor"
	"go-lamp.autonomous.ai/internal/statusled"
	"go-lamp.autonomous.ai/lib/flow"
	"go-lamp.autonomous.ai/lib/i18n"
	"go-lamp.autonomous.ai/lib/lelamp"
	"go-lamp.autonomous.ai/lib/mood"
	"go-lamp.autonomous.ai/lib/musicsuggestion"
	"go-lamp.autonomous.ai/lib/posture"
	"go-lamp.autonomous.ai/lib/sensingmsg"
	"go-lamp.autonomous.ai/lib/usercanon"
	"go-lamp.autonomous.ai/lib/wellbeing"
	"go-lamp.autonomous.ai/server/config"
	"go-lamp.autonomous.ai/server/serializers"
)

// SensingEventRequest is the payload from LeLamp sensing detectors.
type SensingEventRequest struct {
	// Type is the event category: motion, sound, presence.enter, presence.leave, light.level, etc.
	Type string `json:"type" validate:"required"`
	// Message is a natural-language description of what was detected.
	Message string `json:"message" validate:"required"`
	// Image is an optional base64-encoded JPEG snapshot from the camera.
	// Attached automatically for significant events (large motion, face detected) so AI can see.
	Image string `json:"image,omitempty"`
	// CurrentUser is LeLamp's view of who is effectively in front of the lamp
	// right now (from FaceRecognizer.current_user()). Empty when nobody is
	// visible. This is the source of truth — do NOT re-derive by parsing
	// Message. Text parsing gave wrong answers when a stranger-only enter
	// event fired while a friend was still present (Lamp would downgrade
	// mood to "unknown" even though the friend was within forget window).
	CurrentUser string `json:"current_user,omitempty"`
	// Audio is an optional path (on the Pi) to the WAV clip that produced this
	// event — currently only speech_emotion.detected, carrying the latest clip
	// of the dominant label this flush. It is a DEBUG aid surfaced in the Flow
	// Monitor UI as a clickable player; it is NEVER forwarded to the LLM (the
	// field is not part of Message and is never concatenated into the outgoing
	// chat text). Served back to the UI via GetAudio.
	Audio string `json:"audio,omitempty"`
}

// SensingHandler handles incoming sensing events from LeLamp and forwards them to the agent.
type SensingHandler struct {
	agentGateway     domain.AgentGateway
	monitorBus       *monitor.Bus
	config           *config.Config
	statusLED        *statusled.Service
	voiceActiveUntil atomic.Int64 // unix ms; set on voice_listening, extended on voice_listening_end
	isSleeping       func() bool  // returns true when agent last expressed "sleepy" emotion
	lastNotReadyTTS  atomic.Int64 // unix ms; cooldown for "brain restarting" TTS
}

// ProvideSensingHandler constructs a SensingHandler.
func ProvideSensingHandler(gw domain.AgentGateway, bus *monitor.Bus, cfg *config.Config, sled *statusled.Service, isSleeping func() bool) SensingHandler {
	return SensingHandler{agentGateway: gw, monitorBus: bus, config: cfg, statusLED: sled, isSleeping: isSleeping}
}

// PostEvent receives a sensing event and sends it to the agent as a chat message.
// Voice events are first checked against local intent rules for instant response.
func (h *SensingHandler) PostEvent(c *gin.Context) {
	var req SensingEventRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}

	slog.Info("sensing event received", "component", "sensing", "type", req.Type, "message", req.Message)

	// Voice command from physical device — log for tracing (LED feedback is handled by LeLamp).
	if req.Type == "voice_command" {
		slog.Info("voice_command received", "component", "sensing", "message", req.Message)
	}
	// voice_listening / voice_listening_end are internal LED signals — don't forward to agent.
	// Also gate sensing events: suppress passive sensing during the voice conversation window
	// so motion/presence can't steal the agent turn while the user is speaking or waiting for reply.
	if req.Type == "voice_listening" {
		// Extend window: user is speaking, keep sensing suppressed for 10s from now.
		h.voiceActiveUntil.Store(time.Now().Add(10 * time.Second).UnixMilli())
		c.JSON(http.StatusOK, serializers.ResponseSuccess(nil))
		return
	}
	if req.Type == "voice_listening_end" {
		// Extend window 5s to cover STT → Lamp → LLM → TTS pipeline.
		h.voiceActiveUntil.Store(time.Now().Add(5 * time.Second).UnixMilli())
		c.JSON(http.StatusOK, serializers.ResponseSuccess(nil))
		return
	}

	startPayload := map[string]any{"type": req.Type, "message": req.Message}

	// Push sensing input to monitor.
	monitorDetail := map[string]any{"type": req.Type}
	// Surface the debug audio clip (speech_emotion) to the Flow Monitor UI only
	// — as a servable URL, never the raw path, and never to the LLM.
	if audioURL := audioURLForPath(req.Audio); audioURL != "" {
		monitorDetail["audio"] = audioURL
	}
	h.monitorBus.Push(domain.MonitorEvent{
		Type:    "sensing_input",
		Summary: "[" + req.Type + "] " + req.Message,
		Detail:  monitorDetail,
	})

	// Sync mood.CurrentUser with LeLamp's view on every event that carries
	// it. LeLamp's FaceRecognizer.current_user() is the source of truth.
	//
	// Wellbeing enter/leave rows are written by LeLamp directly (per
	// friend on their own timeline, stranger collapsed to "unknown"
	// timeline) — the handler no longer writes them here. See
	// facerecognizer._post_wellbeing.
	if req.CurrentUser != "" {
		mood.SetCurrentUser(req.CurrentUser)
	} else if req.Type == "presence.leave" || req.Type == "presence.away" {
		mood.ClearCurrentUser()
	}

	// Voice commands: try local intent matching first for instant response
	if (req.Type == "voice" || req.Type == "voice_command") && h.config.LocalIntentEnabled() {
		if result := intent.Match(req.Message); result != nil {
			// Generate a dedicated local-intent trace ID so this turn doesn't
			// share the global trace of an in-flight agent turn.
			localRunID := fmt.Sprintf("local-intent-%d", time.Now().UnixMilli())
			turnStart := flow.Start("sensing_input", startPayload, localRunID)
			flow.Log("intent_match", map[string]any{"message": req.Message, "tts": result.TTSText, "rule": result.Rule, "actions": result.Actions}, localRunID)
			if result.TTSText != "" {
				go func() {
					// Cached path: fixed phrases like "Volume up!" hit the
					// WAV cache (~50ms) instead of going through ElevenLabs
					// (~1.5s). Dynamic texts (time, color) miss + render once.
					if err := lelamp.SpeakCached(result.TTSText); err != nil {
						slog.Warn("intent TTS failed", "component", "sensing", "error", err)
					}
				}()
			}
			// Signal ambient service about LED state changes
			if result.LEDChanged {
				h.monitorBus.Push(domain.MonitorEvent{Type: "led_set", Summary: "intent: " + req.Message})
			} else if result.LEDOff {
				h.monitorBus.Push(domain.MonitorEvent{Type: "led_off", Summary: "intent: " + req.Message})
			}
			if result.Emotion != "" {
				h.monitorBus.Push(domain.MonitorEvent{Type: "emotion", Summary: result.Emotion})
			}
			h.monitorBus.Push(domain.MonitorEvent{
				Type:    "intent_match",
				Summary: "[local] " + req.Message + " → " + result.TTSText,
			})
			flow.End("sensing_input", turnStart, map[string]any{"path": "local"}, localRunID)
			c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]string{
				"handler":  "local",
				"response": result.TTSText,
			}))
			return
		}
	}

	// Sleep guard: while the agent is in "sleepy" state, drop all passive sensing
	// (light.level, motion, sound) so they don't wake the agent and override the
	// sleepy emotion. Only presence.enter and voice_command can wake the lamp.
	// web_chat is user-initiated text from the monitor UI — bypasses sleep-drop
	// (forwarded to agent, TTS suppressed) but does NOT trigger physical wake.
	// web_chat counts as passive for busy-gate so it queues on agent busy
	// instead of racing the in-flight turn (agent merges same-session messages).
	isVoice := req.Type == "voice" || req.Type == "voice_command"
	isVoiceCommand := req.Type == "voice_command"
	isWebChat := req.Type == "web_chat"
	isPassive := !isVoiceCommand
	if isPassive && !isVoice && !isWebChat && req.Type != "presence.enter" && h.isSleeping != nil && h.isSleeping() {
		slog.Info("sensing event dropped — sleeping", "component", "sensing", "type", req.Type)
		h.monitorBus.Push(domain.MonitorEvent{
			Type:    "sensing_drop",
			Summary: "[" + req.Type + "] " + req.Message,
			Detail:  map[string]any{"type": req.Type, "reason": "sleeping"},
		})
		c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]string{"handler": "dropped_sleeping"}))
		return
	}

	// Voice wake: when a voice command arrives while sleeping, fire greeting emotion
	// to LeLamp so it wakes up (LED + servo) before the agent processes the turn.
	// Without this, the agent's emotion:thinking would be blocked by LeLamp's wake guard.
	// web_chat skips wake — typing in the monitor isn't a request for physical interaction.
	if isVoiceCommand && h.isSleeping != nil && h.isSleeping() {
		slog.Info("voice wake — firing greeting to wake LeLamp", "component", "sensing")
		go func() {
			if err := lelamp.SetEmotion("greeting", 0.8); err != nil {
				slog.Warn("voice wake greeting failed", "component", "sensing", "error", err)
				return
			}
			slog.Info("voice wake greeting sent", "component", "sensing")
		}()
	}

	// When agent is busy:
	// - voice_command (wake word confirmed) always passes through immediately.
	// - voice (ambient STT), presence.enter/leave are queued and replayed when agent becomes idle.
	// - During voice window: all passive sensing is queued (not dropped) so events aren't lost.
	// - Outside voice window: motion/light/sound dropped when busy (low priority, high frequency).
	inVoiceWindow := time.Now().UnixMilli() < h.voiceActiveUntil.Load()
	if isPassive && h.agentGateway.IsBusy() {
		// motion.activity and emotion.detected get queued (not dropped) because
		// LeLamp deduplicates both with a 5-min window at the source — if one
		// reaches Lamp it's genuinely new. Dropping it here would make LeLamp's
		// dedup think "sent" while the agent never saw the event, blocking the
		// next real transition for 5 min.
		if shouldQueueEvent(req.Type, req.Message, inVoiceWindow) {
			// Pre-allocate runID for web_chat so the web client can correlate
			// SSE events when this turn replays. Other queued types don't need
			// a runID (LeLamp doesn't track them).
			var queuedRunID string
			if isWebChat {
				_, queuedRunID = h.agentGateway.NextChatRunID()
				// TEMP: TTS suppression disabled to test speaker remotely from web chat.
				// h.agentGateway.MarkWebChatRun(queuedRunID)
			}
			h.agentGateway.QueuePendingEvent(req.Type, req.Message, req.Image, queuedRunID)
			resp := map[string]string{"handler": "queued"}
			if queuedRunID != "" {
				resp["runId"] = queuedRunID
			}
			c.JSON(http.StatusOK, serializers.ResponseSuccess(resp))
			return
		}
		slog.Info("sensing event dropped — agent busy", "component", "sensing", "type", req.Type)
		h.monitorBus.Push(domain.MonitorEvent{
			Type:    "sensing_drop",
			Summary: "[" + req.Type + "] " + req.Message,
			Detail:  map[string]any{"type": req.Type, "reason": "agent_busy"},
		})
		c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]string{"handler": "dropped"}))
		return
	}

	// Guard mode: mark the run so SSE handler broadcasts the response via Telegram Bot API.
	guardActive := isPassive && h.config.GuardModeEnabled() && (req.Type == "presence.enter" || req.Type == "motion" || req.Type == "fire_hazard.detected")
	if guardActive {
		slog.Info("guard mode active", "component", "sensing", "type", req.Type)
	}

	// No local match — forward to OpenClaw agent
	if !h.agentGateway.IsReady() {
		notReadyRunID := fmt.Sprintf("not-ready-%d", time.Now().UnixMilli())
		turnStart := flow.Start("sensing_input", startPayload, notReadyRunID)
		flow.End("sensing_input", turnStart, map[string]any{"error": "agent not connected"}, notReadyRunID)
		// Announce once via TTS so user knows the brain is restarting (cooldown 60s).
		if req.Type == "voice_command" || req.Type == "presence.enter" {
			now := time.Now().UnixMilli()
			if last := h.lastNotReadyTTS.Load(); now-last > 60_000 {
				if h.lastNotReadyTTS.CompareAndSwap(last, now) {
					go func() {
						if err := lelamp.Speak(i18n.One(i18n.PhraseBrainRestart)); err != nil {
							slog.Warn("not-ready TTS failed", "component", "sensing", "error", err)
						}
					}()
				}
			}
		}
		c.JSON(http.StatusServiceUnavailable, serializers.ResponseError("agent gateway not connected"))
		return
	}

	// Same run_id as chat.send / JSONL: SetTrace before flow.Start so enter matches this turn (not previous).
	reqID, runID := h.agentGateway.NextChatRunID()
	flow.SetTrace(runID)

	// Mark this run as guard-active so SSE handler broadcasts the agent response via Telegram.
	if guardActive {
		snap := extractSnapshotPath(req.Message)
		h.agentGateway.MarkGuardRun(runID, snap)
	}
	// motion.activity events that fold in a posture nudge ship two extra
	// markers ([pose_bucket: ...] / [pose_worst: ...]). Stash them keyed
	// by runID so the SSE /dm path can attach the worst frames to the
	// Telegram message after the agent decides to nudge.
	//
	// Same trigger also writes a `posture_alert` row to the user's
	// posture JSONL — the habit skill's Flow A reads those rows to
	// derive peak_hour / side_bias / typical_risk. Without this bridge
	// the habit-skill posture extension stays starved (agent only logs
	// nudge/praise; the raw alert signal had no Lamp-side writer).
	if req.Type == "motion.activity" {
		if bid, worst := extractPoseBucketMarkers(req.Message); bid != "" {
			h.agentGateway.MarkPoseBucketRun(runID, bid, worst)
			if alertUser := req.CurrentUser; alertUser != "" || mood.CurrentUser() != "" {
				if alertUser == "" {
					alertUser = mood.CurrentUser()
				}
				if extras, ok := extractPostureAlertExtras(req.Message); ok {
					posture.LogAlert(alertUser, extras)
				}
			}
		}
	}
	// Web monitor chat: suppress TTS — response displayed in web UI only.
	// TEMP: disabled to test TTS remotely from web chat.
	// if isWebChat {
	// 	h.agentGateway.MarkWebChatRun(runID)
	// }
	// Important: pass explicit runID to flow.Start to avoid global trace race (another goroutine may interleave
	// between SetTrace() and Start()).
	turnStart := flow.Start("sensing_input", startPayload, runID)

	// Resolve user attribution: prefer the request payload, fall back to mood.
	// The drain path (service_events.go) snapshots this at queue time; here we
	// resolve fresh per request.
	currentUser := req.CurrentUser
	if currentUser == "" {
		currentUser = mood.CurrentUser()
	}
	// Guard tag is only built on the live path — the queue path always passes
	// "" because guard state isn't preserved across the queue.
	var guardTag string
	if guardActive {
		guardTag = "[sensing:" + req.Type + "][guard-active]"
		if inst := h.config.GuardInstruction; inst != "" {
			guardTag += "[guard-instruction: " + inst + "]"
		}
	}
	msg := sensingmsg.Build(req.Type, req.Message, currentUser, guardTag)

	// Strip [snapshot: ...] markers from the outgoing LLM message. The full text
	// (with snapshot paths) remains in the sensing_input JSONL via startPayload so
	// the Monitor UI can still render thumbnails — the agent just doesn't waste
	// tokens on the path.
	msg = reSnapshotPath.ReplaceAllString(msg, "")
	// Same treatment for the pose bucket markers — file paths are infra,
	// not LLM context. The Monitor UI reads them from the JSONL payload.
	msg = rePoseBucketMarker.ReplaceAllString(msg, "")
	msg = rePoseWorstMarker.ReplaceAllString(msg, "")
	msg = strings.ReplaceAll(msg, "\n\n\n", "\n\n")
	msg = strings.TrimSpace(msg)

	// Web chat with image: save to temp file so agent can reference the path
	// (e.g. for face enrollment). Tag uses [image:] not [snapshot:] to avoid strip.
	if isWebChat && req.Image != "" {
		if imgData, err := base64.StdEncoding.DecodeString(req.Image); err == nil {
			tmpPath := fmt.Sprintf("/tmp/web-chat-%d.jpg", time.Now().UnixMilli())
			if err := os.WriteFile(tmpPath, imgData, 0644); err == nil {
				msg += "\n[image: " + tmpPath + "]"
			}
		}
	}

	// Mark voice turns so the SSE handler can re-arm a Continuation filler
	// at each tool.end. Done before forwarding so the lifecycle.start
	// event can never race ahead of the mark.
	// Other turn types (passive sensing, web chat, guard) deliberately
	// stay unmarked — fillers are voice-only.
	//
	// Opening filler is fired-and-forget IMMEDIATELY here (not via
	// FillerManager timer). This is the pre-2026-05-04 behaviour: filler
	// arrives at lelamp ~5-10s before the LLM real reply, so it has time
	// to synthesize and play out before the real reply arrives — avoiding
	// the lelamp-side speak() lock-timeout=2s race that the timer-based
	// fire-at-lifecycle.start+FillerDelay path triggers.
	// TEMP: include isWebChat so remote TTS test gets the same opening filler
	// + dead-air filler experience as voice. Revert with the MarkWebChatRun
	// suppression toggle above when done — search "TEMP: disabled to test TTS".
	if isVoice || isWebChat {
		DefaultFillerManager.MarkVoiceRun(runID)
		go PlayOpeningFillerNow()
	}

	var err error
	// Web monitor chat starting with "/" is a slash command — forward via
	// chat.send with deliver:false so OpenClaw routes the reply back to the
	// web client only (matches gw web). Without this, slash replies can be
	// swallowed by bound-channel routing and the SSE stream times out.
	isSlashCommand := isWebChat && strings.HasPrefix(msg, "/")
	// motion.activity: snapshot saved for UI but NOT sent to agent (save tokens — action name is enough)
	if req.Image != "" && req.Type != "motion.activity" {
		if isSlashCommand {
			_, err = h.agentGateway.SendSlashCommandWithImageAndRun(msg, req.Image, reqID, runID)
		} else {
			_, err = h.agentGateway.SendChatMessageWithImageAndRun(msg, req.Image, reqID, runID)
		}
	} else {
		if isSlashCommand {
			_, err = h.agentGateway.SendSlashCommandWithRun(msg, reqID, runID)
		} else {
			_, err = h.agentGateway.SendChatMessageWithRun(msg, reqID, runID)
		}
	}

	if err != nil {
		// Forward failed — drop the voice mark so we don't keep state
		// for a run that will never produce a lifecycle.start.
		DefaultFillerManager.Cancel(runID)
		slog.Error("failed to send event", "component", "sensing", "error", err)
		flow.End("sensing_input", turnStart, map[string]any{"error": err.Error()})
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}

	flow.End("sensing_input", turnStart, map[string]any{"path": "agent", "run_id": runID}, runID)
	flow.Log("agent_call", map[string]any{"type": req.Type, "run_id": runID}, runID)

	slog.Info("flow correlation", "op", "lelamp_agent_out", "section", "lelamp_to_openclaw",
		"device_run_id", runID, "sensing_type", req.Type,
		"note", "OpenClaw lifecycle UUID maps to device_run_id on lifecycle_start in SSE handler")
	slog.Info("event forwarded", "component", "sensing", "type", req.Type, "hasImage", req.Image != "", "runId", runID)
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]string{
		"runId": runID,
	}))
}

// MonitorEventRequest is the payload for pushing an event to the monitor bus.
type MonitorEventRequest struct {
	Type    string         `json:"type" validate:"required"`
	Summary string         `json:"summary" validate:"required"`
	Detail  map[string]any `json:"detail,omitempty"`
	RunID   string         `json:"runId,omitempty"`
}

// PostMonitorEvent allows internal services (e.g. LeLamp) to push events to the monitor bus.
func (h *SensingHandler) PostMonitorEvent(c *gin.Context) {
	var req MonitorEventRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	h.monitorBus.Push(domain.MonitorEvent{
		Type:    req.Type,
		Summary: req.Summary,
		Detail:  req.Detail,
		RunID:   req.RunID,
	})
	c.JSON(http.StatusOK, serializers.ResponseSuccess(nil))
}

// EnableGuardRequest is the optional payload for enabling guard mode.
type EnableGuardRequest struct {
	Instruction string `json:"instruction,omitempty"`
}

// EnableGuard activates guard mode with an optional custom instruction.
func (h *SensingHandler) EnableGuard(c *gin.Context) {
	var req EnableGuardRequest
	// Body is optional — ignore bind errors (empty body is fine).
	_ = c.ShouldBindJSON(&req)

	t := true
	h.config.GuardMode = &t
	h.config.GuardInstruction = req.Instruction
	if err := h.config.Save(); err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}
	slog.Info("guard mode enabled", "component", "sensing", "instruction", req.Instruction)
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"guard_mode":  true,
		"instruction": req.Instruction,
	}))
}

// DisableGuard deactivates guard mode and clears any custom instruction.
func (h *SensingHandler) DisableGuard(c *gin.Context) {
	f := false
	h.config.GuardMode = &f
	h.config.GuardInstruction = ""
	if err := h.config.Save(); err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}
	slog.Info("guard mode disabled", "component", "sensing")
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]bool{"guard_mode": false}))
}

// GetGuardStatus returns the current guard mode state.
func (h *SensingHandler) GetGuardStatus(c *gin.Context) {
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]bool{
		"guard_mode": h.config.GuardModeEnabled(),
	}))
}

// GuardAlertRequest is the payload for manually triggering a guard broadcast.
type GuardAlertRequest struct {
	Message string `json:"message" validate:"required"`
	Image   string `json:"image,omitempty"`
}

// PostGuardAlert broadcasts an alert message to all chat sessions (manual alerts only).
func (h *SensingHandler) PostGuardAlert(c *gin.Context) {
	var req GuardAlertRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	var imagePath string
	if req.Image != "" {
		if data, err := base64.StdEncoding.DecodeString(req.Image); err == nil {
			tmp := filepath.Join(os.TempDir(), fmt.Sprintf("guard-alert-%d.jpg", time.Now().UnixMilli()))
			if err := os.WriteFile(tmp, data, 0644); err == nil {
				imagePath = tmp
				defer os.Remove(tmp)
			}
		}
	}
	if err := h.agentGateway.Broadcast(req.Message, imagePath); err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(nil))
}

// GetSnapshot serves a sensing snapshot image.
// LeLamp writes snapshots as <dir>/<category>/<name>, where <category> is
// sensing_<prefix> (e.g. sensing_motion_activity) and <name> is <ms>.jpg.
// Checks persistent dir first (/var/lib/lelamp/snapshots/), falls back to tmp.
func (h *SensingHandler) GetSnapshot(c *gin.Context) {
	category := c.Param("category")
	name := c.Param("name")
	validCategory := strings.HasPrefix(category, "sensing_") ||
		strings.HasPrefix(category, "emotion_") ||
		strings.HasPrefix(category, "motion_")
	if !validCategory || strings.ContainsAny(category, "/\\") || strings.Contains(category, "..") {
		c.Status(http.StatusNotFound)
		return
	}
	if !strings.HasSuffix(name, ".jpg") || strings.ContainsAny(name, "/\\") || strings.Contains(name, "..") {
		c.Status(http.StatusNotFound)
		return
	}
	persistPath := filepath.Join("/var/lib/lelamp/snapshots", category, name)
	if _, err := os.Stat(persistPath); err == nil {
		c.File(persistPath)
		return
	}
	for _, dir := range []string{
		"/tmp/lamp-sensing-snapshots",
		"/tmp/lamp-emotion-snapshots",
		"/tmp/lamp-motion-snapshots",
	} {
		p := filepath.Join(dir, category, name)
		if _, err := os.Stat(p); err == nil {
			c.File(p)
			return
		}
	}
	c.Status(http.StatusNotFound)
}

// speechEmotionAudioDirs are the on-Pi locations where the speech_emotion
// service writes its debug WAV clips (mirrors LELAMP_SPEECH_EMOTION_AUDIO_DIR
// default + a persistent fallback). GetAudio serves files by basename from
// these dirs only.
var speechEmotionAudioDirs = []string{
	"/var/lib/lelamp/speech-emotion",
	"/tmp/lamp-speech-emotion",
}

// audioURLForPath maps a raw on-Pi WAV path (from SensingEventRequest.Audio)
// to a UI-servable URL, or "" when the path is empty / not a .wav. Only the
// basename is exposed so the full filesystem path never leaks to the UI.
func audioURLForPath(path string) string {
	if path == "" {
		return ""
	}
	name := filepath.Base(path)
	if !strings.HasSuffix(name, ".wav") {
		return ""
	}
	return "/api/sensing/audio/" + name
}

// GetAudio serves a speech_emotion debug WAV clip by basename. This is a
// debug-only affordance for the Flow Monitor UI; the audio is never sent to
// the LLM. The speech_emotion service names clips <ms>_<user>_<label>.wav with
// user/label sanitized to [a-zA-Z0-9_-], so a strict basename check suffices.
func (h *SensingHandler) GetAudio(c *gin.Context) {
	name := c.Param("name")
	if !strings.HasSuffix(name, ".wav") || strings.ContainsAny(name, "/\\") || strings.Contains(name, "..") {
		c.Status(http.StatusNotFound)
		return
	}
	for _, dir := range speechEmotionAudioDirs {
		p := filepath.Join(dir, name)
		if _, err := os.Stat(p); err == nil {
			c.File(p)
			return
		}
	}
	c.Status(http.StatusNotFound)
}

// MoodLogRequest is the payload for logging a user mood event.
//
// kind="signal" (default): raw evidence from one source. Source + Trigger required.
// kind="decision": agent-synthesized mood. BasedOn + Reasoning recommended;
//
//	Source defaults to "agent". Trigger is ignored.
type MoodLogRequest struct {
	Mood      string `json:"mood" validate:"required"`                        // happy, sad, stressed, tired, excited, etc.
	Kind      string `json:"kind" validate:"omitempty,oneof=signal decision"` // signal (default) or decision
	Source    string `json:"source"`                                          // signal: camera|voice|telegram|conversation. Required for signals.
	Trigger   string `json:"trigger"`                                         // signal: action/context. Required for signals.
	BasedOn   string `json:"based_on"`                                        // decision only: short summary of inputs
	Reasoning string `json:"reasoning"`                                       // decision only: why this mood
	User      string `json:"user"`                                            // optional: agent passes when it knows (e.g. Telegram sender)
}

// PostMoodLog records a mood signal or decision row to the user's history.
func (h *SensingHandler) PostMoodLog(c *gin.Context) {
	var req MoodLogRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}

	kind := req.Kind
	if kind == "" {
		kind = mood.KindSignal
	}
	if kind == mood.KindSignal && (strings.TrimSpace(req.Source) == "" || strings.TrimSpace(req.Trigger) == "") {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("signal requires source and trigger"))
		return
	}

	user := req.User
	if strings.TrimSpace(user) == "" {
		user = mood.CurrentUser()
	}
	user = usercanon.Resolve(user)

	evt := mood.Event{
		Kind:      kind,
		Mood:      req.Mood,
		Source:    req.Source,
		Trigger:   req.Trigger,
		BasedOn:   req.BasedOn,
		Reasoning: req.Reasoning,
	}
	if kind == mood.KindDecision {
		evt.Trigger = ""
		if evt.Source == "" {
			evt.Source = "agent"
		}
	}
	mood.LogEvent(user, evt)
	slog.Info("mood logged", "component", "mood", "user", user, "kind", kind, "mood", req.Mood, "source", evt.Source, "trigger", evt.Trigger, "based_on", evt.BasedOn)

	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]string{
		"user": user,
		"kind": kind,
		"mood": req.Mood,
	}))
}

// WellbeingLogRequest is the payload for logging a wellbeing activity.
// Accepted actions:
//   - Bucket names (agent writes from motion.activity hybrid output): drink, break
//   - Raw Kinetics sedentary labels (agent writes verbatim from motion.activity):
//     using computer, writing, texting, reading book, reading newspaper, drawing,
//     playing controller
//   - Nudge records (agent writes after speaking): nudge_hydration, nudge_break
//   - Presence markers (backend writes internally): enter, leave
//
// The enum is intentionally permissive for `action` — validator only requires a
// non-empty, short string. The log is append-only; semantic checks (what counts
// as a reset point for hydration/break) live in the Wellbeing SKILL.
type WellbeingLogRequest struct {
	Action string `json:"action" validate:"required,max=64"`
	Notes  string `json:"notes"`
	User   string `json:"user"`
}

// PostWellbeingLog appends a wellbeing activity entry for the given user.
func (h *SensingHandler) PostWellbeingLog(c *gin.Context) {
	var req WellbeingLogRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}

	user := req.User
	if strings.TrimSpace(user) == "" {
		user = mood.CurrentUser()
	}
	user = usercanon.Resolve(user)

	wellbeing.LogForUser(user, req.Action, req.Notes)
	slog.Info("wellbeing logged", "component", "wellbeing", "user", user, "action", req.Action, "notes", req.Notes)
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]string{
		"user":   user,
		"action": req.Action,
	}))
}

// --- Posture History API ---

// PostureLogRequest is the JSON body the agent / HW marker dispatcher sends to
// /api/posture/log. `action` is one of the constants in lib/posture (alert,
// nudge, praise); only the fields relevant to that action are expected.
type PostureLogRequest struct {
	Action     string `json:"action" validate:"required"`
	NudgeLevel int    `json:"nudge_level,omitempty"`
	Score      int    `json:"score,omitempty"`
	Risk       string `json:"risk,omitempty"`
	LeftScore  int    `json:"left_score,omitempty"`
	RightScore int    `json:"right_score,omitempty"`
	Notes      string `json:"notes,omitempty"`
	User       string `json:"user"`
}

// PostPostureLog appends a posture-history row. Dispatches to LogAlert /
// LogNudge / LogPraise depending on `action`; unknown actions return 400.
func (h *SensingHandler) PostPostureLog(c *gin.Context) {
	var req PostureLogRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}

	user := req.User
	if strings.TrimSpace(user) == "" {
		user = mood.CurrentUser()
	}
	user = usercanon.Resolve(user)

	switch req.Action {
	case posture.ActionAlert:
		posture.LogAlert(user, posture.AlertExtras{
			Score:      req.Score,
			Risk:       req.Risk,
			LeftScore:  req.LeftScore,
			RightScore: req.RightScore,
		})
	case posture.ActionNudge:
		posture.LogNudge(user, req.NudgeLevel, req.Notes)
	case posture.ActionPraise:
		posture.LogPraise(user, req.Notes)
	default:
		c.JSON(http.StatusBadRequest, serializers.ResponseError("unknown posture action: "+req.Action))
		return
	}

	slog.Info("posture logged", "component", "posture", "user", user, "action", req.Action, "level", req.NudgeLevel)
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]string{
		"user":   user,
		"action": req.Action,
	}))
}

// --- Guard helpers ---

// Trailing \n? so stripping [snapshot:...] from a multi-line message doesn't
// leave a blank line behind. Capture group ([^\]]+) is unchanged so
// extractSnapshotPath still pulls the file path via FindStringSubmatch.
var reSnapshotPath = regexp.MustCompile(`\[snapshot:\s*([^\]]+)\]\n?`)

// Pose bucket markers — emitted by lelamp motion.py when a posture nudge
// rides along on motion.activity. They reference a lelamp-side bucket
// dir + the pre-selected worst-snapshot filenames, NOT a base64 image
// payload, so they're cheap to keep in the JSONL.
var rePoseBucketMarker = regexp.MustCompile(`\[pose_bucket:\s*([^\]]+)\]\n?`)
var rePoseWorstMarker = regexp.MustCompile(`\[pose_worst:\s*([^\]]+)\]\n?`)

// extractSnapshotPath extracts the snapshot file path from a sensing message.
func extractSnapshotPath(message string) string {
	m := reSnapshotPath.FindStringSubmatch(message)
	if m == nil {
		return ""
	}
	return strings.TrimSpace(m[1])
}

// extractPostureSummaryJSON locates the [posture_summary: …] marker
// and returns just the JSON object body (without the marker brackets).
// posture_summary nests two levels deep (`latest_left.body_scores`)
// and also contains array literals (`skipped_joints:[]`), both of
// which a flat regex would mishandle — walk braces manually instead.
func extractPostureSummaryJSON(message string) string {
	const tag = "[posture_summary:"
	i := strings.Index(message, tag)
	if i < 0 {
		return ""
	}
	start := strings.IndexByte(message[i:], '{')
	if start < 0 {
		return ""
	}
	start += i
	depth := 0
	for j := start; j < len(message); j++ {
		switch message[j] {
		case '{':
			depth++
		case '}':
			depth--
			if depth == 0 {
				return message[start : j+1]
			}
		}
	}
	return ""
}

// riskLevelLabel maps the dlbackend RULA risk_level enum to the string
// vocabulary the habit skill expects on posture_alert rows.
func riskLevelLabel(level int) string {
	switch level {
	case 4:
		return "high"
	case 3:
		return "medium"
	case 2:
		return "low"
	case 1:
		return "negligible"
	default:
		return ""
	}
}

// extractPostureAlertExtras parses the [posture_summary: ...] JSON
// payload on a motion.activity message and translates the fields the
// habit skill needs onto a posture.AlertExtras.
//
// Source-of-truth fields are the `worst_*` keys (lelamp pre-computes
// them as the max across the same 3 samples it surfaces for the DM
// gallery), so habit numbers stay aligned with the photos the user
// just saw. Falls back to `latest_*` when an older lelamp build is
// still in the field, so a half-rolled deploy doesn't drop alert
// rows entirely.
//
// Returns ok=false when neither set of fields carries score/risk —
// habit can tolerate sparse history but shouldn't see rows missing
// both pieces of ergonomic data.
func extractPostureAlertExtras(message string) (posture.AlertExtras, bool) {
	body := extractPostureSummaryJSON(message)
	if body == "" {
		return posture.AlertExtras{}, false
	}
	var s struct {
		WorstScore      int `json:"worst_score"`
		WorstRiskLevel  int `json:"worst_risk_level"`
		WorstLeftScore  int `json:"worst_left_score"`
		WorstRightScore int `json:"worst_right_score"`

		LatestScore     int `json:"latest_score"`
		LatestRiskLevel int `json:"latest_risk_level"`
		LatestLeft      struct {
			Score int `json:"score"`
		} `json:"latest_left"`
		LatestRight struct {
			Score int `json:"score"`
		} `json:"latest_right"`
	}
	if err := json.Unmarshal([]byte(body), &s); err != nil {
		return posture.AlertExtras{}, false
	}

	score := s.WorstScore
	risk := s.WorstRiskLevel
	left := s.WorstLeftScore
	right := s.WorstRightScore
	if score == 0 && risk == 0 {
		// Older lelamp builds (or any path where the worst aggregate
		// wasn't computed) — fall back to the last-sample values so
		// the alert row still lands.
		score = s.LatestScore
		risk = s.LatestRiskLevel
		left = s.LatestLeft.Score
		right = s.LatestRight.Score
	}
	if score == 0 && risk == 0 {
		return posture.AlertExtras{}, false
	}
	return posture.AlertExtras{
		Score:      score,
		Risk:       riskLevelLabel(risk),
		LeftScore:  left,
		RightScore: right,
	}, true
}

// extractPoseBucketMarkers pulls (bucket_id, [worst filenames]) from a
// motion.activity message. Returns empty bucket_id when the markers are
// absent (most motion.activity turns — no posture nudge folded in).
func extractPoseBucketMarkers(message string) (string, []string) {
	bm := rePoseBucketMarker.FindStringSubmatch(message)
	if bm == nil {
		return "", nil
	}
	bucketID := strings.TrimSpace(bm[1])
	if bucketID == "" {
		return "", nil
	}
	wm := rePoseWorstMarker.FindStringSubmatch(message)
	var worst []string
	if wm != nil {
		for _, part := range strings.Split(wm[1], ",") {
			part = strings.TrimSpace(part)
			if part != "" {
				worst = append(worst, part)
			}
		}
	}
	return bucketID, worst
}

// --- Music Suggestion History API ---

type MusicSuggestionLogRequest struct {
	User    string `json:"user" validate:"required"`
	Trigger string `json:"trigger" validate:"required"`
	Query   string `json:"query"`
	Message string `json:"message" validate:"required"`
}

// PostMusicSuggestionLog records a music suggestion event.
func (h *SensingHandler) PostMusicSuggestionLog(c *gin.Context) {
	var req MusicSuggestionLogRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}

	user := usercanon.Resolve(req.User)
	seq := musicsuggestion.Log(user, req.Trigger, req.Query, req.Message)
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"user": user,
		"seq":  seq,
		"day":  time.Now().Format("2006-01-02"),
	}))
}

type MusicSuggestionStatusRequest struct {
	User   string `json:"user" validate:"required"`
	Day    string `json:"day" validate:"required"`
	Seq    int64  `json:"seq" validate:"required"`
	Status string `json:"status" validate:"required,oneof=accepted rejected expired"`
}

// PostMusicSuggestionStatus updates the status of a previously logged music suggestion.
func (h *SensingHandler) PostMusicSuggestionStatus(c *gin.Context) {
	var req MusicSuggestionStatusRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}

	user := usercanon.Resolve(req.User)
	ok := musicsuggestion.UpdateStatus(user, req.Day, req.Seq, req.Status)
	if !ok {
		c.JSON(http.StatusNotFound, serializers.ResponseError("music suggestion not found"))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(nil))
}

// shouldQueueEvent returns true if this sensing event type should be queued
// (not dropped) when the agent is busy.
func shouldQueueEvent(eventType, message string, inVoiceWindow bool) bool {
	switch eventType {
	case "presence.enter", "presence.leave", "voice",
		"motion.activity", "emotion.detected", "speech_emotion.detected",
		"web_chat":
		return true
	case "sound":
		return strings.Contains(message, "persistent")
	default:
		return inVoiceWindow
	}
}

// VoiceFileRemoveRequest deletes ONE voice sample file from a user's
// /root/local/users/<name>/voice/ folder. Used by the Voice Enroll UI's
// per-file delete button. After deletion the speaker embedding is
// recomputed by calling /speaker/enroll with the remaining WAV files;
// if no WAVs remain we POST /speaker/remove to drop the whole profile.
type VoiceFileRemoveRequest struct {
	Name string `json:"name" validate:"required"`
	File string `json:"file" validate:"required"`
}

const usersDir = "/root/local/users"

func (h *SensingHandler) RemoveVoiceFile(c *gin.Context) {
	var req VoiceFileRemoveRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	name := strings.ToLower(strings.TrimSpace(req.Name))
	file := strings.TrimSpace(req.File)
	// Path traversal guard — file must be a bare filename, no separators
	// or ".." components. The voice dir layout is flat.
	if name == "" || file == "" || strings.ContainsAny(file, "/\\") || file == "." || file == ".." {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid name or file"))
		return
	}
	// Only allow deleting audio samples — json/npy are critical state for
	// speaker_recognizer (metadata, embedding cache); deleting them silently
	// corrupts the profile. UI hides Delete for these too; this is the
	// belt-and-braces guard.
	switch strings.ToLower(filepath.Ext(file)) {
	case ".wav", ".ogg", ".mp3", ".webm", ".m4a":
	default:
		c.JSON(http.StatusBadRequest, serializers.ResponseError("only audio samples can be deleted"))
		return
	}

	voiceDir := filepath.Join(usersDir, name, "voice")
	target := filepath.Join(voiceDir, file)
	// Belt-and-braces: resolved path must stay under voiceDir.
	absVoice, err1 := filepath.Abs(voiceDir)
	absTarget, err2 := filepath.Abs(target)
	if err1 != nil || err2 != nil || !strings.HasPrefix(absTarget+string(filepath.Separator), absVoice+string(filepath.Separator)) {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid path"))
		return
	}
	if _, err := os.Stat(target); err != nil {
		c.JSON(http.StatusNotFound, serializers.ResponseError("file not found"))
		return
	}
	if err := os.Remove(target); err != nil {
		slog.Warn("voice file remove failed", "component", "voice", "path", target, "error", err)
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("delete failed: "+err.Error()))
		return
	}
	slog.Info("voice file deleted", "component", "voice", "name", name, "file", file)

	// Find remaining WAVs (only WAV files matter to speaker_recognizer).
	entries, _ := os.ReadDir(voiceDir)
	remainingWavs := []string{}
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		if strings.HasSuffix(strings.ToLower(e.Name()), ".wav") {
			remainingWavs = append(remainingWavs, filepath.Join(voiceDir, e.Name()))
		}
	}

	// No WAVs left → remove the speaker profile entirely so list endpoints
	// don't show a phantom user with 0 samples.
	if len(remainingWavs) == 0 {
		body, _ := json.Marshal(map[string]any{"name": name})
		resp, err := http.Post("http://127.0.0.1:5001/speaker/remove", "application/json", bytes.NewReader(body))
		if err != nil {
			slog.Warn("speaker/remove call failed", "component", "voice", "error", err)
		} else {
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
		}
		c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
			"deleted": file,
			"profile": "removed",
		}))
		return
	}

	// Re-enroll with the remaining WAVs so speaker_recognizer recomputes
	// the embedding from what's actually on disk.
	body, _ := json.Marshal(map[string]any{
		"name":      name,
		"wav_paths": remainingWavs,
		"origin":    "web_recompute",
	})
	resp, err := http.Post("http://127.0.0.1:5001/speaker/enroll", "application/json", bytes.NewReader(body))
	if err != nil {
		slog.Warn("speaker/enroll recompute failed", "component", "voice", "error", err)
		c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
			"deleted": file,
			"warning": "embedding not recomputed: " + err.Error(),
		}))
		return
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		slog.Warn("speaker/enroll recompute returned error", "component", "voice", "status", resp.StatusCode, "body", string(respBody))
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"deleted":   file,
		"remaining": len(remainingWavs),
	}))
}
