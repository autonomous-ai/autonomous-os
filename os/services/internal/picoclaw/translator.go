package picoclaw

import (
	"encoding/json"
	"log/slog"
	"strings"
	"time"

	"go.autonomous.ai/os/domain"
)

// nowUnixMs returns the current time in milliseconds (matches the OpenClaw frame
// timestamp convention).
func nowUnixMs() int64 { return time.Now().UnixMilli() }

// picoFrame is one inbound PicoClaw message. The discriminator is Type; for
// message.create / message.update the category is decided by the payload fields
// (placeholder / kind / tool_calls / content) — never by Type alone.
type picoFrame struct {
	Type      string      `json:"type"`
	SessionID string      `json:"session_id"`
	Timestamp int64       `json:"timestamp"`
	Payload   picoPayload `json:"payload"`
}

type picoPayload struct {
	Content     string         `json:"content"`
	MessageID   string         `json:"message_id"`
	Placeholder bool           `json:"placeholder"`
	Kind        string         `json:"kind"`
	Thought     bool           `json:"thought"` // legacy reasoning flag
	ModelName   string         `json:"model_name"`
	ToolCalls   []picoToolCall `json:"tool_calls"`
	Usage       *picoUsage     `json:"context_usage"`

	// error frames
	Code    string `json:"code"`
	Message string `json:"message"`
}

type picoToolCall struct {
	ID       string `json:"id"`
	Type     string `json:"type"`
	Function struct {
		Name      string `json:"name"`
		Arguments string `json:"arguments"` // JSON string, not an object
	} `json:"function"`
}

// picoUsage is PicoClaw's context_usage block (only present on the final frame).
type picoUsage struct {
	UsedTokens    int `json:"used_tokens"`
	TotalTokens   int `json:"total_tokens"`
	HistoryTokens int `json:"history_tokens"`
	UsedPercent   int `json:"used_percent"`
}

func (u *picoUsage) toDomain() *domain.TokenUsage {
	if u == nil {
		return nil
	}
	if u.UsedTokens == 0 && u.TotalTokens == 0 && u.HistoryTokens == 0 {
		return nil
	}
	// PicoClaw reports cumulative context size, not per-turn input/output. Map
	// the running context size onto TotalTokens and the carried history onto
	// InputTokens so the monitor's token gauge has meaningful numbers.
	return &domain.TokenUsage{
		InputTokens: u.HistoryTokens,
		TotalTokens: u.UsedTokens,
	}
}

// category classifies a message.create / message.update payload.
type category int

const (
	catOther    category = iota // empty / not a renderable message
	catThinking                 // placeholder ("Thinking...") or reasoning (kind=thought)
	catTool                     // tool_calls
	catFinal                    // real final answer (ends the turn)
)

func categorize(p picoPayload) category {
	if p.Placeholder {
		return catThinking
	}
	if strings.EqualFold(strings.TrimSpace(p.Kind), "thought") || p.Thought {
		return catThinking
	}
	if strings.EqualFold(strings.TrimSpace(p.Kind), "tool_calls") || len(p.ToolCalls) > 0 {
		return catTool
	}
	if strings.TrimSpace(p.Content) != "" {
		return catFinal
	}
	return catOther
}

// translateFrame parses one inbound PicoClaw frame and emits 0..N domain.WSEvent
// frames into dispatch. Mapping (keep in sync with docs/agentic/picoclaw.md):
//
//	typing.start                    → lifecycle.start (once per turn)
//	message.create/update placeholder/thought → ignored (state, not content)
//	message.create kind=tool_calls  → tool.start + tool.end per call
//	message.create/update final     → chat.final + lifecycle.end (ends turn)
//	error                           → lifecycle.error (ends turn)
//	typing.stop / message.delete / pong → ignored
func (s *Service) translateFrame(raw []byte, dispatch func(domain.WSEvent)) {
	var f picoFrame
	if err := json.Unmarshal(raw, &f); err != nil {
		slog.Debug("picoclaw: non-JSON frame, ignored", "component", "picoclaw", "raw", truncRunes(string(raw), 200))
		return
	}

	// Capture the server-assigned session_id from any frame.
	if f.SessionID != "" && f.SessionID != s.GetSessionKey() {
		s.SetSessionKey(f.SessionID)
	}

	switch f.Type {
	case "typing.start":
		s.ensureTurnStarted(dispatch)
	case "typing.stop", "message.delete", "pong":
		// typing.stop arrives BEFORE the final answer (right after the thinking
		// phase) — it is NOT the end of the turn. message.delete removes the
		// "Thinking..." placeholder we never rendered. pong is keepalive. Ignore.
	case "error":
		s.handleError(f, dispatch)
	case "message.create", "message.update":
		s.handleMessage(f, dispatch)
	default:
		slog.Debug("picoclaw: unhandled frame type", "component", "picoclaw", "type", f.Type)
	}
}

func (s *Service) handleMessage(f picoFrame, dispatch func(domain.WSEvent)) {
	switch categorize(f.Payload) {
	case catThinking:
		// Open the turn so the lifecycle is consistent, but render nothing —
		// "Thinking..." / reasoning is status, not the answer.
		s.ensureTurnStarted(dispatch)
	case catTool:
		s.ensureTurnStarted(dispatch)
		s.emitToolCalls(f, dispatch)
	case catFinal:
		s.ensureTurnStarted(dispatch)
		s.emitFinal(f, dispatch)
	case catOther:
		// empty content, nothing to do
	}
}

// ensureTurnStarted emits lifecycle.start exactly once per turn. The runID is
// adopted from a pending outbound SendChat when present, else freshly allocated
// for an externally-initiated turn (e.g. a Telegram message PicoClaw processed).
func (s *Service) ensureTurnStarted(dispatch func(domain.WSEvent)) {
	if s.getCurrentRunID() != "" {
		return // already started
	}
	runID := s.consumePendingRunID()
	if runID == "" {
		_, runID = s.NextChatRunID()
	}
	s.setCurrentRunID(runID)
	if !s.activeTurn.Load() {
		s.busySince.Store(nowUnixMs())
		s.activeTurn.Store(true)
	}

	slog.Info("picoclaw <<< turn started", "component", "picoclaw", "runID", runID, "sessionKey", s.GetSessionKey())
	payload, _ := json.Marshal(map[string]any{
		"runId":      runID,
		"sessionKey": s.GetSessionKey(),
		"stream":     "lifecycle",
		"data": map[string]any{
			"phase":     "start",
			"startedAt": nowUnixMs(),
		},
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: payload})
}

// emitToolCalls surfaces each OpenAI-style tool call as a tool.start + tool.end
// pair. PicoClaw reports calls after the fact and does not stream a separate
// result frame, so tool.end carries an empty result purely to close the trace.
func (s *Service) emitToolCalls(f picoFrame, dispatch func(domain.WSEvent)) {
	runID := s.getCurrentRunID()
	for _, c := range f.Payload.ToolCalls {
		name := c.Function.Name
		if name == "" {
			name = "tool"
		}
		args := c.Function.Arguments
		callID := c.ID
		slog.Info("picoclaw <<< tool CALL", "component", "picoclaw",
			"runID", runID, "tool", name, "toolCallId", callID, "argsLen", len(args))

		startPayload, _ := json.Marshal(map[string]any{
			"runId":      runID,
			"sessionKey": s.GetSessionKey(),
			"stream":     "tool",
			"data": map[string]any{
				"phase":      "start",
				"name":       name,
				"toolCallId": callID,
				"arguments":  args,
			},
		})
		dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: startPayload})

		endPayload, _ := json.Marshal(map[string]any{
			"runId":      runID,
			"sessionKey": s.GetSessionKey(),
			"stream":     "tool",
			"data": map[string]any{
				"phase":      "end",
				"toolCallId": callID,
				"result":     "",
			},
		})
		dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: endPayload})
	}
}

// emitFinal emits (a) the final chat message and (b) lifecycle.end with usage,
// then closes the turn. Order matches OpenClaw/Hermes so handler_events.go sees
// chat.final before lifecycle.end → idle.
//
// The turn ids are reset BEFORE dispatch: the consumer (handler_event_chat.go /
// handler_event_agent.go) calls SetBusy(false) on chat.final and lifecycle.end,
// which synchronously drains queued sensing events and starts the NEXT turn
// (fresh pendingRunID). Clearing here lets that turn's runID survive instead of
// being clobbered. Busy itself is owned by the consumer's SetBusy(false) — the
// translator never touches it (matches the Hermes translator).
func (s *Service) emitFinal(f picoFrame, dispatch func(domain.WSEvent)) {
	runID := s.getCurrentRunID()
	finalText := f.Payload.Content

	logArgs := []any{
		"component", "picoclaw",
		"runID", runID,
		"finalLen", len(finalText),
		"final", truncRunes(finalText, 500),
	}
	if f.Payload.Usage != nil {
		logArgs = append(logArgs, "usedTokens", f.Payload.Usage.UsedTokens, "usedPercent", f.Payload.Usage.UsedPercent)
	}
	slog.Info("picoclaw <<< final answer", logArgs...)

	s.currentRunID.Store("")
	s.pendingRunID.Store("")

	chatMsg, _ := json.Marshal(map[string]any{
		"runId":      runID,
		"sessionKey": s.GetSessionKey(),
		"state":      "final",
		"role":       "assistant",
		"message":    finalText,
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "chat", Payload: chatMsg})

	endPayload, _ := json.Marshal(map[string]any{
		"runId":      runID,
		"sessionKey": s.GetSessionKey(),
		"stream":     "lifecycle",
		"data": map[string]any{
			"phase":   "end",
			"endedAt": nowUnixMs(),
			"usage":   f.Payload.Usage.toDomain(),
		},
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: endPayload})
}

func (s *Service) handleError(f picoFrame, dispatch func(domain.WSEvent)) {
	s.ensureTurnStarted(dispatch) // make sure a runID exists for the error
	runID := s.getCurrentRunID()
	msg := f.Payload.Message
	if msg == "" {
		msg = f.Payload.Code
	}
	if msg == "" {
		msg = "picoclaw error"
	}
	slog.Warn("picoclaw <<< error", "component", "picoclaw", "runID", runID, "code", f.Payload.Code, "error", msg)

	// Reset turn ids before dispatch (see emitFinal) — the consumer clears busy
	// on lifecycle.error, draining the next turn synchronously.
	s.currentRunID.Store("")
	s.pendingRunID.Store("")

	payload, _ := json.Marshal(map[string]any{
		"runId":      runID,
		"sessionKey": s.GetSessionKey(),
		"stream":     "lifecycle",
		"data": map[string]any{
			"phase":   "error",
			"error":   msg,
			"endedAt": nowUnixMs(),
		},
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: payload})
}

// --- turn-correlation helpers ---

func (s *Service) getCurrentRunID() string {
	v, _ := s.currentRunID.Load().(string)
	return v
}

func (s *Service) setCurrentRunID(runID string) { s.currentRunID.Store(runID) }

func (s *Service) setPendingRunID(runID string) { s.pendingRunID.Store(runID) }

func (s *Service) consumePendingRunID() string {
	v, _ := s.pendingRunID.Load().(string)
	if v != "" {
		s.pendingRunID.Store("")
	}
	return v
}

// clearTurn resets the in-flight turn ids without touching busy state. Used on
// disconnect / busyTTL expiry / send failure. The normal end-of-turn path clears
// the ids inline in emitFinal / handleError (before dispatch) so a drained
// follow-up turn's ids survive — see emitFinal.
func (s *Service) clearTurn() {
	s.currentRunID.Store("")
	s.pendingRunID.Store("")
}
