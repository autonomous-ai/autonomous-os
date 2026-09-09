package server

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/harness"
	"go.autonomous.ai/os/system/server/serializers"
)

type harnessReply struct {
	runID   string
	webChat bool
	created time.Time
}

type harnessReplyRequest struct {
	RunID   string `json:"run_id"`
	Channel string `json:"channel"`
}

// registerHarnessRoutes exposes management to the owner and commands only to the device runtime.
func (s *Server) registerHarnessRoutes(api *gin.RouterGroup, ctx context.Context) {
	group := api.Group("harness")
	group.Use(func(c *gin.Context) {
		if s.harnessService == nil {
			c.AbortWithStatusJSON(http.StatusServiceUnavailable, serializers.ResponseError("Harness service unavailable"))
		}
	})
	group.GET("status", adminOrLoopbackAuth(s.config), func(c *gin.Context) {
		status := s.harnessService.Status()
		// Pairing codes are secrets for the pairing flow; only the authenticated
		// MQTT pairing channel may carry the live code to the mobile owner.
		status.Code = ""
		status.ExpiresAt = 0
		c.JSON(http.StatusOK, serializers.ResponseSuccess(status))
	})
	group.GET("voice-followup", localOnlyMiddleware(), func(c *gin.Context) {
		c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"active": s.HarnessVoiceFollowup()}))
	})
	// Pairing codes and pinned E2EE identities authenticate the direct socket.
	group.GET("ws", func(c *gin.Context) {
		s.harnessService.ServeHTTP(c.Writer, c.Request)
	})
	group.GET("pair/status", adminAuthMiddleware(s.config), func(c *gin.Context) {
		c.Header("Cache-Control", "no-store")
		c.JSON(http.StatusOK, serializers.ResponseSuccess(s.harnessService.PairStatus()))
	})
	group.POST("pair", adminAuthMiddleware(s.config), func(c *gin.Context) {
		info, err := s.harnessService.StartPair(ctx)
		if err != nil {
			c.JSON(http.StatusConflict, serializers.ResponseError(err.Error()))
			return
		}
		c.Header("Cache-Control", "no-store")
		c.JSON(http.StatusAccepted, serializers.ResponseSuccess(info))
	})
	group.POST("pair/cancel", adminAuthMiddleware(s.config), func(c *gin.Context) {
		if err := s.harnessService.CancelPair(); err != nil {
			c.JSON(http.StatusInternalServerError, serializers.ResponseError("Could not cancel Harness pairing"))
			return
		}
		c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"cancelled": true}))
	})
	group.DELETE("", adminAuthMiddleware(s.config), func(c *gin.Context) {
		if err := s.harnessService.Unpair(); err != nil {
			c.JSON(http.StatusInternalServerError, serializers.ResponseError("Could not remove Harness pairing"))
			return
		}
		c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"unpaired": true}))
	})
	group.POST("request", localOnlyMiddleware(), func(c *gin.Context) {
		var frame harness.Frame
		c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 64*1024)
		if err := c.ShouldBindJSON(&frame); err != nil || frame == nil {
			c.JSON(http.StatusBadRequest, serializers.ResponseError("Invalid Harness request"))
			return
		}
		reply, err := extractHarnessReply(frame)
		if err != nil {
			c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
			return
		}
		kind, _ := frame["type"].(string)
		agentID, _ := frame["agentId"].(string)
		tracksReply := (kind == "turn.send" || kind == "question.answer") && reply != nil
		// A local Harness agent can complete before Request returns its receipt.
		// Install the local route first so an immediate terminal event is not lost.
		if tracksReply {
			s.registerHarnessReply(agentID, reply.RunID, reply.Channel == "web")
		}
		requestCtx, cancel := context.WithTimeout(c.Request.Context(), 35*time.Second)
		defer cancel()
		result, err := s.harnessService.Request(requestCtx, frame)
		if err != nil {
			if tracksReply {
				s.forgetHarnessReply(agentID, reply.RunID)
			}
			var uncertain *harness.DeliveryUnknownError
			if errors.As(err, &uncertain) {
				c.JSON(http.StatusBadGateway, serializers.ResponseError("Harness delivery is unknown; inspect receipt before sending again"))
				return
			}
			c.JSON(http.StatusBadGateway, serializers.ResponseError(err.Error()))
			return
		}
		c.JSON(http.StatusOK, serializers.ResponseSuccess(result))
	})
}

func extractHarnessReply(frame harness.Frame) (*harnessReplyRequest, error) {
	raw, present := frame["response"]
	delete(frame, "response") // local routing metadata is never sent to Harness.
	if !present {
		return nil, nil
	}
	encoded, ok := raw.(map[string]any)
	if !ok {
		return nil, errors.New("Invalid Harness response routing")
	}
	reply := &harnessReplyRequest{}
	if value, ok := encoded["run_id"].(string); ok {
		reply.RunID = strings.TrimSpace(value)
	}
	if value, ok := encoded["channel"].(string); ok {
		reply.Channel = strings.TrimSpace(value)
	}
	if reply.RunID == "" || len(reply.RunID) > 128 || (reply.Channel != "voice" && reply.Channel != "web") {
		return nil, errors.New("Invalid Harness response routing")
	}
	return reply, nil
}

func (s *Server) registerHarnessReply(agentID, runID string, webChat bool) {
	if agentID == "" || runID == "" || s.agentHandler == nil {
		return
	}
	s.harnessRepliesMu.Lock()
	if s.harnessReplies == nil {
		s.harnessReplies = make(map[string]harnessReply)
	}
	// The remote Harness run id may be returned in the routing object by older
	// skills. Preserve the local device turn mapping when that happens; replacing
	// it would deliver the terminal event to a nonexistent device-chat run.
	if current, exists := s.harnessReplies[agentID]; exists && !strings.HasPrefix(runID, "device-chat-") {
		runID, webChat = current.runID, current.webChat
	}
	s.harnessReplies[agentID] = harnessReply{runID: runID, webChat: webChat, created: time.Now()}
	s.harnessRepliesMu.Unlock()
	s.harnessFollowup.Store(time.Now().Add(2 * time.Minute).UnixMilli())
	s.agentHandler.MarkHarnessResponseRun(runID, webChat)
}

func (s *Server) forgetHarnessReply(agentID, runID string) {
	s.harnessRepliesMu.Lock()
	if current, ok := s.harnessReplies[agentID]; ok && current.runID == runID {
		delete(s.harnessReplies, agentID)
	}
	s.harnessRepliesMu.Unlock()
}

// HarnessVoiceFollowup keeps short spoken clarifications with the paired agent.
func (s *Server) HarnessVoiceFollowup() bool {
	return time.Now().UnixMilli() < s.harnessFollowup.Load()
}

// HarnessFollowupContext returns the latest direct Harness result while its
// follow-up window is open. It is injected as untrusted context into the next
// user turn so the device agent can answer a clarification without pretending
// it generated or observed the remote result itself.
func (s *Server) HarnessFollowupContext() string {
	if !s.HarnessVoiceFollowup() {
		return ""
	}
	s.harnessResultMu.RLock()
	defer s.harnessResultMu.RUnlock()
	if time.Since(s.harnessResultAt) > 15*time.Minute {
		return ""
	}
	return s.harnessResult
}

func (s *Server) rememberHarnessResult(text string) {
	text = strings.TrimSpace(text)
	if text == "" {
		return
	}
	s.harnessResultMu.Lock()
	s.harnessResult = text
	s.harnessResultAt = time.Now()
	s.harnessResultMu.Unlock()
	s.harnessFollowup.Store(time.Now().Add(2 * time.Minute).UnixMilli())
}

// forwardHarnessEvent relays real Harness lifecycle events to the original
// device interaction. The terminal recap is not passed through the device
// agent, which would otherwise add a slower, altered second answer.
func (s *Server) forwardHarnessEvent(frame harness.Frame) {
	agentID, _ := frame["agentId"].(string)
	if agentID == "" {
		// Some Harness transports identify the originating request by run ID
		// instead of agent ID. Resolve that directly to the pending device turn.
		runID, _ := frame["runId"].(string)
		if runID == "" {
			runID, _ = frame["run_id"].(string)
		}
		if payload, ok := frame["payload"].(map[string]any); ok && runID == "" {
			runID, _ = payload["runId"].(string)
			if runID == "" {
				runID, _ = payload["run_id"].(string)
			}
		}
		if runID != "" {
			s.harnessRepliesMu.Lock()
			for id, reply := range s.harnessReplies {
				if reply.runID == runID {
					agentID = id
					break
				}
			}
			s.harnessRepliesMu.Unlock()
		}
	}
	if agentID == "" {
		agentID, _ = frame["agent_id"].(string)
	}
	if agentID == "" {
		if payload, ok := frame["payload"].(map[string]any); ok {
			agentID, _ = payload["agentId"].(string)
			if agentID == "" {
				agentID, _ = payload["agent_id"].(string)
			}
		}
	}
	if agentID == "" {
		return
	}

	kind, _ := frame["kind"].(string)
	if kind == "turn.tool" {
		toolName, toolArgs := harnessToolEvent(frame)
		if toolName == "" {
			return
		}
		s.harnessRepliesMu.Lock()
		reply, ok := s.harnessReplies[agentID]
		s.harnessRepliesMu.Unlock()
		if ok && time.Since(reply.created) <= 15*time.Minute {
			s.agentHandler.DeliverHarnessTool(reply.runID, toolName, toolArgs)
		}
		return
	}
	text := harnessEventText(kind, frame)
	if text == "" && kind != "turn.summary" {
		return
	}
	s.harnessRepliesMu.Lock()
	reply, ok := s.harnessReplies[agentID]
	// Some CLI versions include a stale or target agentId in the event while
	// preserving the originating run_id. Prefer the run mapping when the direct
	// agent lookup misses so Web/MQTT turns receive the same terminal event as
	// voice turns.
	if !ok {
		if runID := harnessFrameRunID(frame); runID != "" {
			for id, candidate := range s.harnessReplies {
				if candidate.runID == runID {
					agentID, reply, ok = id, candidate, true
					break
				}
			}
		}

	}
	terminal := kind == "turn.summary" || kind == "turn.error" || kind == "agent.error" || kind == "question.open"
	s.harnessRepliesMu.Unlock()
	if !ok || time.Since(reply.created) > 15*time.Minute {
		return
	}
	if !terminal {
		s.agentHandler.DeliverHarnessProgress(reply.runID, text)
		return
	}
	if kind == "turn.summary" {
		// The event's text is the compact device-card preview. Fetch the
		// corresponding recap before delivering so Web Chat and TTS receive the
		// complete user-facing result retained by Harness.
		text = s.harnessRecapText(agentID, text)
	}
	if strings.TrimSpace(text) == "" {
		slog.Warn("Harness summary has no result yet", "component", "harness", "run_id", reply.runID, "agent_id", agentID)
		return
	}
	// Recap is fetched outside the lock. Do not delete a newer response route
	// registered while that request was in flight.
	s.forgetHarnessReply(agentID, reply.runID)
	s.rememberHarnessResult(text)
	if !s.agentHandler.DeliverHarnessResponse(reply.runID, text) {
		slog.Warn("Harness result had no pending device turn", "component", "harness", "run_id", reply.runID)
	}
}

func harnessFrameRunID(frame harness.Frame) string {
	for _, key := range []string{"runId", "run_id"} {
		if value, _ := frame[key].(string); value != "" {
			return value
		}
	}
	if payload, ok := frame["payload"].(map[string]any); ok {
		for _, key := range []string{"runId", "run_id"} {
			if value, _ := payload[key].(string); value != "" {
				return value
			}
		}
	}
	return ""
}

func harnessToolEvent(frame harness.Frame) (name, args string) {
	payload, _ := frame["payload"].(map[string]any)
	if payload == nil {
		return "", ""
	}
	name, _ = payload["text"].(string)
	if detail, _ := payload["detail"].(string); detail != "" {
		args = detail
	} else {
		args, _ = payload["recap"].(string)
	}
	return strings.TrimSpace(name), strings.TrimSpace(args)
}

// harnessRecapText reads the complete final text stored by Harness. Summary
// events deliberately carry a compact preview for device tiles, so that
// preview is used only if the read-only recap request cannot be completed.
func (s *Server) harnessRecapText(agentID, fallback string) string {
	if s.harnessService == nil || agentID == "" {
		return fallback
	}
	machineID := s.harnessService.Status().MachineID
	if machineID == "" {
		return fallback
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	frame, err := s.harnessService.Request(ctx, harness.Frame{
		"type":      "recap",
		"machineId": machineID,
		"agentId":   agentID,
		"n":         1,
	})
	if err != nil {
		slog.Debug("Harness recap unavailable; using summary preview", "component", "harness", "agent_id", agentID, "error", err)
		return fallback
	}
	if text := harnessRecapResultText(frame); text != "" {
		return text
	}
	return fallback
}

// harnessRecapResultText extracts the latest complete user-facing message
// from the read-only recap result. fullText is supplied by current Harness
// CLIs for readers such as voice; text is the compact legacy preview.
func harnessRecapResultText(frame harness.Frame) string {
	turns, _ := frame["turns"].([]any)
	for _, raw := range turns {
		turn, _ := raw.(map[string]any)
		if fullText, _ := turn["fullText"].(string); strings.TrimSpace(fullText) != "" {
			return strings.TrimSpace(fullText)
		}
		if text, _ := turn["text"].(string); strings.TrimSpace(text) != "" {
			return strings.TrimSpace(text)
		}
	}
	return ""
}

func harnessEventText(kind string, frame harness.Frame) string {
	payload, _ := frame["payload"].(map[string]any)
	if payload == nil {
		return ""
	}
	if kind == "receipt.updated" {
		receipt, _ := payload["receipt"].(map[string]any)
		state, _ := receipt["state"].(string)
		switch state {
		case "queued", "delivered":
			return "Harness accepted the request."
		case "started":
			return "Harness agent is working."
		}
		return ""
	}
	if kind == "turn.started" {
		return "Harness agent is working."
	}
	if kind == "turn.done" {
		return "Harness agent finished; receiving its result."
	}
	if kind == "question.open" {
		questions, _ := payload["questions"].([]any)
		for _, raw := range questions {
			question, _ := raw.(map[string]any)
			for _, key := range []string{"question", "prompt", "text"} {
				if text, _ := question[key].(string); strings.TrimSpace(text) != "" {
					return strings.TrimSpace(text)
				}
			}
		}
		return "Harness needs an answer before it can continue."
	}
	if kind == "turn.error" {
		return "Harness stopped before it returned a result."
	}
	if kind == "turn.summary" {
		if fullText, _ := payload["fullText"].(string); strings.TrimSpace(fullText) != "" {
			return strings.TrimSpace(fullText)
		}
	}
	text, _ := payload["text"].(string)
	if kind == "turn.summary" && strings.TrimSpace(text) == "" {
		text, _ = payload["recap"].(string)
	}
	if kind != "turn.summary" && kind != "turn.error" && kind != "agent.error" {
		return ""
	}
	return strings.TrimSpace(text)
}
