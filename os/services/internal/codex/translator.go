package codex

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

// codexFrame is one inbound frame from the bridge: either a Codex exec JSONL
// event forwarded verbatim (thread.started / turn.* / item.*) or a bridge
// frame (bridge.status / bridge.error / pong).
type codexFrame struct {
	Type     string     `json:"type"`
	ThreadID string     `json:"thread_id"` // thread.started, bridge.status
	Item     codexItem  `json:"item"`      // item.started / item.completed
	Usage    *codexUse  `json:"usage"`     // turn.completed
	Error    codexError `json:"error"`     // turn.failed (object), bridge.error (string) — see UnmarshalJSON
	Message  string     `json:"message"`   // error events
	State    string     `json:"state"`     // bridge.status
}

// codexError tolerates both shapes seen on the wire: turn.failed carries an
// object {"message": "..."}, bridge.error carries a plain string.
type codexError struct {
	Message string
}

func (e *codexError) UnmarshalJSON(raw []byte) error {
	var s string
	if json.Unmarshal(raw, &s) == nil {
		e.Message = s
		return nil
	}
	var obj struct {
		Message string `json:"message"`
	}
	if json.Unmarshal(raw, &obj) == nil {
		e.Message = obj.Message
	}
	return nil
}

// codexItem is one output item of a turn. Codex has emitted the discriminator
// as both "item_type" and "type" across versions — accept either.
type codexItem struct {
	ID       string `json:"id"`
	ItemType string `json:"item_type"`
	Type     string `json:"type"`

	// agent_message / reasoning
	Text string `json:"text"`

	// command_execution
	Command          string `json:"command"`
	AggregatedOutput string `json:"aggregated_output"`
	ExitCode         *int   `json:"exit_code"`

	// mcp_tool_call
	Server string `json:"server"`
	Tool   string `json:"tool"`

	// web_search
	Query string `json:"query"`

	// file_change
	Changes json.RawMessage `json:"changes"`

	Status string `json:"status"`
}

func (it codexItem) kind() string {
	if it.ItemType != "" {
		return it.ItemType
	}
	return it.Type
}

// codexUse is the turn.completed usage block.
type codexUse struct {
	InputTokens       int `json:"input_tokens"`
	CachedInputTokens int `json:"cached_input_tokens"`
	OutputTokens      int `json:"output_tokens"`
}

// toDomain maps Codex usage onto domain.TokenUsage. input+cached approximates
// the live context size (what ShouldRotateSession keys on); TotalTokens is the
// full turn volume.
func (u *codexUse) toDomain() *domain.TokenUsage {
	if u == nil {
		return nil
	}
	in := u.InputTokens + u.CachedInputTokens
	if in == 0 && u.OutputTokens == 0 {
		return nil
	}
	return &domain.TokenUsage{
		InputTokens:  in,
		OutputTokens: u.OutputTokens,
		TotalTokens:  in + u.OutputTokens,
	}
}

// translateFrame parses one inbound bridge frame and emits 0..N domain.WSEvent
// frames into dispatch. Mapping (keep in sync with docs/agentic/codex.md):
//
//	thread.started                → capture session key, lifecycle.start
//	turn.started                  → lifecycle.start (once per turn)
//	item.started  command/mcp     → tool.start
//	item.completed command/mcp    → tool.end (start first when unseen)
//	item.completed web_search /
//	               file_change    → tool.start + tool.end pair
//	item.completed agent_message  → accumulate final text (no delta stream)
//	item.* reasoning / todo_list  → ignored
//	turn.completed                → delta(final) + chat.final + lifecycle.end
//	turn.failed / error /
//	bridge.error                  → lifecycle.error (ends turn)
//	bridge.status / pong          → log / ignore
func (s *CodexService) translateFrame(raw []byte, dispatch func(domain.WSEvent)) {
	var f codexFrame
	if err := json.Unmarshal(raw, &f); err != nil {
		slog.Debug("codex: non-JSON frame, ignored", "component", "codex", "raw", truncRunes(string(raw), 200))
		return
	}

	// Capture the thread id from any frame that carries one.
	if f.ThreadID != "" && f.ThreadID != s.GetSessionKey() {
		s.SetSessionKey(f.ThreadID)
	}

	switch f.Type {
	case "thread.started", "turn.started":
		s.ensureTurnStarted(dispatch)
	case "item.started", "item.updated":
		s.handleItemStarted(f, dispatch)
	case "item.completed":
		s.handleItemCompleted(f, dispatch)
	case "turn.completed":
		s.emitFinal(f, dispatch)
	case "turn.failed":
		s.handleError(f.Error.Message, dispatch)
	case "error":
		msg := f.Message
		if msg == "" {
			msg = f.Error.Message
		}
		s.handleError(msg, dispatch)
	case "bridge.error":
		s.handleError(f.Error.Message, dispatch)
	case "bridge.status":
		slog.Info("codex bridge status", "component", "codex", "state", f.State, "threadId", f.ThreadID)
	case "pong":
		// keepalive reply — ignore
	default:
		slog.Debug("codex: unhandled frame type", "component", "codex", "type", f.Type)
	}
}

// handleItemStarted opens the turn and surfaces tool.start for the item kinds
// that model a tool invocation.
func (s *CodexService) handleItemStarted(f codexFrame, dispatch func(domain.WSEvent)) {
	s.ensureTurnStarted(dispatch)
	switch f.Item.kind() {
	case "command_execution":
		// ensureToolStart (not emitToolStart): item.updated re-delivers the same
		// item id and must not duplicate the tool.start node.
		s.ensureToolStart(f.Item.ID, "shell", f.Item.Command, dispatch)
	case "mcp_tool_call":
		s.ensureToolStart(f.Item.ID, mcpToolName(f.Item), "", dispatch)
	}
}

// handleItemCompleted routes a finished item. agent_message text is
// accumulated (codex exec sends it whole — there is no delta stream) and the
// consumer gets everything at turn.completed, matching the picoclaw N=1
// streaming contract.
func (s *CodexService) handleItemCompleted(f codexFrame, dispatch func(domain.WSEvent)) {
	s.ensureTurnStarted(dispatch)
	it := f.Item
	switch it.kind() {
	case "agent_message":
		if strings.TrimSpace(it.Text) == "" {
			return
		}
		s.turnMu.Lock()
		s.assistantParts = append(s.assistantParts, it.Text)
		s.turnMu.Unlock()
	case "command_execution":
		s.ensureToolStart(it.ID, "shell", it.Command, dispatch)
		s.emitToolEnd(it.ID, truncRunes(it.AggregatedOutput, 400), dispatch)
	case "mcp_tool_call":
		s.ensureToolStart(it.ID, mcpToolName(it), "", dispatch)
		s.emitToolEnd(it.ID, it.Status, dispatch)
	case "web_search":
		s.ensureToolStart(it.ID, "web_search", it.Query, dispatch)
		s.emitToolEnd(it.ID, "", dispatch)
	case "file_change":
		s.ensureToolStart(it.ID, "file_changes", string(it.Changes), dispatch)
		s.emitToolEnd(it.ID, it.Status, dispatch)
	case "reasoning", "todo_list":
		// thinking / plan bookkeeping — status, not content
	default:
		slog.Debug("codex: unhandled item kind", "component", "codex", "kind", it.kind())
	}
}

func mcpToolName(it codexItem) string {
	name := it.Tool
	if it.Server != "" {
		name = it.Server + "." + it.Tool
	}
	if name == "" || name == "." {
		return "mcp_tool"
	}
	return name
}

// ensureTurnStarted emits lifecycle.start exactly once per turn. The runID is
// adopted from a pending outbound SendChat when present, else freshly
// allocated for an externally-initiated turn.
func (s *CodexService) ensureTurnStarted(dispatch func(domain.WSEvent)) {
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
	s.turnMu.Lock()
	s.assistantParts = nil
	s.toolStartSeen = make(map[string]bool)
	s.turnMu.Unlock()

	slog.Info("codex <<< turn started", "component", "codex", "runID", runID, "threadId", s.GetSessionKey())
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

// ensureToolStart emits tool.start when it was not already emitted for this
// item id (an item.completed can arrive without a prior item.started).
func (s *CodexService) ensureToolStart(id, name, args string, dispatch func(domain.WSEvent)) {
	s.turnMu.Lock()
	seen := s.toolStartSeen != nil && s.toolStartSeen[id]
	s.turnMu.Unlock()
	if !seen {
		s.emitToolStart(id, name, args, dispatch)
	}
}

func (s *CodexService) emitToolStart(id, name, args string, dispatch func(domain.WSEvent)) {
	s.turnMu.Lock()
	if s.toolStartSeen == nil {
		s.toolStartSeen = make(map[string]bool)
	}
	s.toolStartSeen[id] = true
	s.turnMu.Unlock()

	runID := s.getCurrentRunID()
	slog.Info("codex <<< tool CALL", "component", "codex",
		"runID", runID, "tool", name, "toolCallId", id, "argsLen", len(args))
	payload, _ := json.Marshal(map[string]any{
		"runId":      runID,
		"sessionKey": s.GetSessionKey(),
		"stream":     "tool",
		"data": map[string]any{
			"phase":      "start",
			"name":       name,
			"toolCallId": id,
			"arguments":  args,
		},
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: payload})
}

func (s *CodexService) emitToolEnd(id, result string, dispatch func(domain.WSEvent)) {
	payload, _ := json.Marshal(map[string]any{
		"runId":      s.getCurrentRunID(),
		"sessionKey": s.GetSessionKey(),
		"stream":     "tool",
		"data": map[string]any{
			"phase":      "end",
			"toolCallId": id,
			"result":     result,
		},
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: payload})
}

// emitFinal emits, in order: (a) the whole reply as a single assistant delta,
// (b) the final chat message, (c) lifecycle.end with usage — then closes the
// turn. Order matches OpenClaw/Hermes/PicoClaw (assistant deltas → chat.final
// → lifecycle.end → idle); codex exec does not stream tokens, so (a) is the
// N=1 case of that contract — it is what lets the shared consumer flush TTS +
// [HW:/…] hardware markers at lifecycle.end.
//
// The turn ids are reset BEFORE dispatch: the consumer calls SetBusy(false)
// on chat.final / lifecycle.end, which synchronously drains queued sensing
// events and starts the NEXT turn (fresh pendingRunID). Clearing here lets
// that turn's runID survive instead of being clobbered.
func (s *CodexService) emitFinal(f codexFrame, dispatch func(domain.WSEvent)) {
	s.ensureTurnStarted(dispatch)
	runID := s.getCurrentRunID()

	s.turnMu.Lock()
	finalText := strings.Join(s.assistantParts, "\n\n")
	s.assistantParts = nil
	s.toolStartSeen = nil
	s.turnMu.Unlock()

	logArgs := []any{
		"component", "codex",
		"runID", runID,
		"finalLen", len(finalText),
		"final", truncRunes(finalText, 500),
	}
	if f.Usage != nil {
		logArgs = append(logArgs,
			"inputTokens", f.Usage.InputTokens,
			"cachedInputTokens", f.Usage.CachedInputTokens,
			"outputTokens", f.Usage.OutputTokens)
	}
	slog.Info("codex <<< turn completed", logArgs...)

	// Clear ONLY currentRunID: pendingRunID belongs to the NEXT turn (it was
	// already consumed at ensureTurnStarted for this one) — a sendChat that
	// queued while this turn streamed must keep its runID so silent/web-chat
	// markers still match.
	s.currentRunID.Store("")

	if finalText != "" {
		deltaPayload, _ := json.Marshal(map[string]any{
			"runId":      runID,
			"sessionKey": s.GetSessionKey(),
			"stream":     "assistant",
			"data":       map[string]any{"delta": finalText},
		})
		dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: deltaPayload})
	}

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
			"usage":   f.Usage.toDomain(),
		},
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: endPayload})
}

func (s *CodexService) handleError(msg string, dispatch func(domain.WSEvent)) {
	s.ensureTurnStarted(dispatch) // make sure a runID exists for the error
	runID := s.getCurrentRunID()
	if msg == "" {
		msg = "codex error"
	}
	slog.Warn("codex <<< error", "component", "codex", "runID", runID, "error", msg)

	// Reset turn ids before dispatch (see emitFinal) — the consumer clears busy
	// on lifecycle.error, draining the next turn synchronously.
	// Clear ONLY currentRunID: pendingRunID belongs to the NEXT turn (it was
	// already consumed at ensureTurnStarted for this one) — a sendChat that
	// queued while this turn streamed must keep its runID so silent/web-chat
	// markers still match.
	s.currentRunID.Store("")
	s.turnMu.Lock()
	s.assistantParts = nil
	s.toolStartSeen = nil
	s.turnMu.Unlock()

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

func (s *CodexService) getCurrentRunID() string {
	v, _ := s.currentRunID.Load().(string)
	return v
}

func (s *CodexService) setCurrentRunID(runID string) { s.currentRunID.Store(runID) }

func (s *CodexService) setPendingRunID(runID string) { s.pendingRunID.Store(runID) }

func (s *CodexService) consumePendingRunID() string {
	v, _ := s.pendingRunID.Load().(string)
	if v != "" {
		s.pendingRunID.Store("")
	}
	return v
}

// clearTurn resets the in-flight turn ids without touching busy state. Used on
// disconnect / busyTTL expiry / send failure. The normal end-of-turn path
// clears the ids inline in emitFinal / handleError (before dispatch).
func (s *CodexService) clearTurn() {
	// Clear ONLY currentRunID: pendingRunID belongs to the NEXT turn (it was
	// already consumed at ensureTurnStarted for this one) — a sendChat that
	// queued while this turn streamed must keep its runID so silent/web-chat
	// markers still match.
	s.currentRunID.Store("")
	s.turnMu.Lock()
	s.assistantParts = nil
	s.toolStartSeen = nil
	s.turnMu.Unlock()
}
