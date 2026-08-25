package opencode

import (
	"encoding/json"
	"log/slog"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
)

// nowUnixMs returns the current time in milliseconds (matches the OpenClaw frame
// timestamp convention).
func nowUnixMs() int64 { return time.Now().UnixMilli() }

// opencodeFrame is one inbound frame from the bridge: an `opencode run
// --format json` JSONL event forwarded verbatim (text / reasoning / tool_use /
// step_start / step_finish / message.updated / session.error) or a bridge frame
// (session.idle synthesized on clean exit / bridge.status / bridge.error / pong).
// Every opencode line carries sessionID.
//
// Device-verified event shapes (opencode 1.18.4): the assistant reply arrives on
// a `text` event under `part.text`; a turn ends with a `step_finish` whose
// `part.reason == "stop"` carrying `part.tokens` (there is NO session.idle from
// the CLI — the gatewayd synthesizes one on the process's clean exit, §gatewayd).
type opencodeFrame struct {
	Type      string `json:"type"`
	SessionID string `json:"sessionID"`
	Status    string `json:"status"` // session.status

	// text / reasoning content (flat "text", or nested "part.text"); step_finish
	// carries the finish reason + per-step token usage under "part".
	Text string `json:"text"`
	Part *struct {
		Text   string          `json:"text"`
		Reason string          `json:"reason"` // step_finish: "stop" = turn done
		Tokens *opencodeTokens `json:"tokens"`
	} `json:"part"`

	// tool_use: call id + tool name + input args (accept several spellings).
	ID    string          `json:"id"`
	Tool  string          `json:"tool"`
	Name  string          `json:"name"`
	Input json.RawMessage `json:"input"`
	Args  json.RawMessage `json:"arguments"`

	// message.updated: the AssistantMessage (tokens/finish), when emitted.
	Info *opencodeInfo `json:"info"`

	// session.error / error: object {"message":..} or a plain string.
	Error   opencodeError `json:"error"`
	Message string        `json:"message"`
}

// textContent returns the assistant text of a text event (flat or nested).
func (f opencodeFrame) textContent() string {
	if strings.TrimSpace(f.Text) != "" {
		return f.Text
	}
	if f.Part != nil {
		return f.Part.Text
	}
	return ""
}

// toolName / toolArgs pick the first populated field name opencode uses.
func (f opencodeFrame) toolName() string {
	if f.Tool != "" {
		return f.Tool
	}
	if f.Name != "" {
		return f.Name
	}
	return "tool"
}

func (f opencodeFrame) toolArgs() string {
	if len(f.Input) > 0 {
		return string(f.Input)
	}
	if len(f.Args) > 0 {
		return string(f.Args)
	}
	return ""
}

// opencodeTokens is the token-usage block opencode carries under a step_finish
// part (part.tokens) and under a message.updated info (info.tokens).
type opencodeTokens struct {
	Input     int `json:"input"`
	Output    int `json:"output"`
	Reasoning int `json:"reasoning"`
	Cache     struct {
		Read  int `json:"read"`
		Write int `json:"write"`
	} `json:"cache"`
}

// opencodeInfo mirrors the fields of opencode's AssistantMessage carried on a
// message.updated event: token usage + the finish reason.
type opencodeInfo struct {
	Tokens *opencodeTokens `json:"tokens"`
	Finish string          `json:"finish"`
}

// opencodeError tolerates the shapes seen on the wire: session.error carries an
// object {"message": ".."} (optionally nested under data), or a plain string.
type opencodeError struct {
	Message string
}

func (e *opencodeError) UnmarshalJSON(raw []byte) error {
	var s string
	if json.Unmarshal(raw, &s) == nil {
		e.Message = s
		return nil
	}
	var obj struct {
		Message string `json:"message"`
		Name    string `json:"name"`
		Data    struct {
			Message string `json:"message"`
		} `json:"data"`
	}
	if json.Unmarshal(raw, &obj) == nil {
		switch {
		case obj.Message != "":
			e.Message = obj.Message
		case obj.Data.Message != "":
			e.Message = obj.Data.Message
		default:
			e.Message = obj.Name
		}
	}
	return nil
}

// captureUsage stashes the latest per-turn token counts from a step_finish
// (part.tokens — device-verified shape) or message.updated (info.tokens) frame;
// emitFinal reads it (the synthesized terminal session.idle carries no usage).
// input + cache.read approximates the live context size (what ShouldRotateSession
// keys on); TotalTokens is the full turn volume.
func (s *OpenCodeService) captureUsage(f opencodeFrame) {
	var tk *opencodeTokens
	switch {
	case f.Part != nil && f.Part.Tokens != nil:
		tk = f.Part.Tokens
	case f.Info != nil && f.Info.Tokens != nil:
		tk = f.Info.Tokens
	default:
		return
	}
	in := tk.Input + tk.Cache.Read
	out := tk.Output
	if in == 0 && out == 0 {
		return
	}
	s.turnMu.Lock()
	s.lastUsage = &domain.TokenUsage{InputTokens: in, OutputTokens: out, TotalTokens: in + out}
	s.turnMu.Unlock()
}

// translateFrame parses one inbound bridge frame and emits 0..N domain.WSEvent
// frames into dispatch. Mapping (keep in sync with docs/agentic/opencode.md):
//
//	first line w/ sessionID        → capture session key
//	step_start                     → lifecycle.start (once per turn)
//	text                           → buffer as the reply (no delta stream); a
//	                                 newer part demotes the previous to thinking
//	                                 unless it carries a [HW:…] marker
//	reasoning                      → ignored (thinking, not content)
//	tool_use                       → tool.start + tool.end pair
//	step_finish / message.updated  → capture token usage
//	session.idle / status idle     → delta(final) + chat.final + lifecycle.end
//	session.error / error /
//	bridge.error                   → lifecycle.error (ends turn)
//	bridge.status / pong           → log / ignore
func (s *OpenCodeService) translateFrame(raw []byte, dispatch func(domain.WSEvent)) {
	var f opencodeFrame
	if err := json.Unmarshal(raw, &f); err != nil {
		slog.Debug("opencode: non-JSON frame, ignored", "component", "opencode", "raw", truncRunes(string(raw), 200))
		return
	}

	// Capture the session id from any frame that carries one.
	if f.SessionID != "" && f.SessionID != s.GetSessionKey() {
		s.SetSessionKey(f.SessionID)
	}

	switch f.Type {
	case "step_start":
		s.ensureTurnStarted(dispatch)
	case "text":
		s.ensureTurnStarted(dispatch)
		if txt := f.textContent(); strings.TrimSpace(txt) != "" {
			// Like codex exec, opencode narrates before it calls a tool ("Using
			// the sensing skill for this presence event.") as its own `text`
			// part. Only the LAST one is the reply; every earlier one is
			// narration that must never reach TTS, so it is demoted to the
			// thinking stream (Flow Monitor only) as soon as a newer part
			// proves it was not the reply. Exception: a part carrying a
			// [HW:…] marker is a real hardware action and is kept.
			s.turnMu.Lock()
			var preamble string
			if n := len(s.assistantParts); n > 0 && !hasHWMarker(s.assistantParts[n-1]) {
				preamble = s.assistantParts[n-1]
				s.assistantParts = s.assistantParts[:n-1]
			}
			s.assistantParts = append(s.assistantParts, txt)
			s.turnMu.Unlock()
			if preamble != "" {
				s.emitThinking(preamble, dispatch)
			}
		}
	case "reasoning":
		// thinking — status, not content (matches codex/hermes)
	case "tool_use":
		s.ensureTurnStarted(dispatch)
		s.ensureToolStart(f.ID, f.toolName(), f.toolArgs(), dispatch)
		s.emitToolEnd(f.ID, "", dispatch)
	case "step_finish", "message.updated":
		s.captureUsage(f)
	case "session.idle":
		s.emitFinal(dispatch)
	case "session.status":
		if f.Status == "idle" {
			s.emitFinal(dispatch)
		}
	case "session.error", "error":
		msg := f.Message
		if msg == "" {
			msg = f.Error.Message
		}
		s.handleError(msg, dispatch)
	case "bridge.error":
		s.handleError(f.Error.Message, dispatch)
	case "bridge.status":
		slog.Info("opencode bridge status", "component", "opencode", "sessionId", f.SessionID)
	case "pong":
		// keepalive reply — ignore
	default:
		slog.Debug("opencode: unhandled frame type", "component", "opencode", "type", f.Type)
	}
}

// ensureTurnStarted emits lifecycle.start exactly once per turn. The runID is
// adopted from a pending outbound SendChat when present, else freshly
// allocated for an externally-initiated turn.
func (s *OpenCodeService) ensureTurnStarted(dispatch func(domain.WSEvent)) {
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
	s.lastUsage = nil
	s.turnMu.Unlock()

	slog.Info("opencode <<< turn started", "component", "opencode", "runID", runID, "sessionId", s.GetSessionKey())
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
func (s *OpenCodeService) ensureToolStart(id, name, args string, dispatch func(domain.WSEvent)) {
	s.turnMu.Lock()
	seen := s.toolStartSeen != nil && s.toolStartSeen[id]
	s.turnMu.Unlock()
	if !seen {
		s.emitToolStart(id, name, args, dispatch)
	}
}

// hasHWMarker reports whether text carries an inline hardware marker, in either
// the plain form `[HW:/led/off]` or the markdown-link form
// `[Lights off](HW:/led/off)`. Deliberately coarse: it only decides whether a
// non-final text part is narration (droppable) or a real action to keep — the
// authoritative parse lives in server/agent/delivery/http/handler_hw.go.
func hasHWMarker(text string) bool {
	return strings.Contains(text, "[HW:") || strings.Contains(text, "](HW:")
}

// emitThinking surfaces text on the thinking stream, which the OS server routes
// to Flow Monitor only — never to TTS or a channel reply.
func (s *OpenCodeService) emitThinking(text string, dispatch func(domain.WSEvent)) {
	runID := s.getCurrentRunID()
	slog.Info("opencode <<< preamble demoted to thinking", "component", "opencode",
		"runID", runID, "text", truncRunes(text, 200))
	payload, _ := json.Marshal(map[string]any{
		"runId":      runID,
		"sessionKey": s.GetSessionKey(),
		"stream":     "thinking",
		"data": map[string]any{
			"text": text,
		},
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: payload})
}

func (s *OpenCodeService) emitToolStart(id, name, args string, dispatch func(domain.WSEvent)) {
	s.turnMu.Lock()
	if s.toolStartSeen == nil {
		s.toolStartSeen = make(map[string]bool)
	}
	s.toolStartSeen[id] = true
	s.turnMu.Unlock()

	runID := s.getCurrentRunID()
	slog.Info("opencode <<< tool CALL", "component", "opencode",
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

func (s *OpenCodeService) emitToolEnd(id, result string, dispatch func(domain.WSEvent)) {
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
// → lifecycle.end → idle); `opencode run` json mode delivers text as discrete
// events (accumulated across the turn), so (a) is the N=1 case of that contract
// — it is what lets the shared consumer flush TTS + [HW:/…] hardware markers at
// lifecycle.end. Usage comes from the stashed lastUsage (a message.updated /
// step_finish frame), since the terminal session.idle carries none.
//
// The turn ids are reset BEFORE dispatch: the consumer calls SetBusy(false)
// on chat.final / lifecycle.end, which synchronously drains queued sensing
// events and starts the NEXT turn (fresh pendingRunID). Clearing here lets
// that turn's runID survive instead of being clobbered.
func (s *OpenCodeService) emitFinal(dispatch func(domain.WSEvent)) {
	s.ensureTurnStarted(dispatch)
	runID := s.getCurrentRunID()

	s.turnMu.Lock()
	finalText := strings.Join(s.assistantParts, "\n\n")
	s.assistantParts = nil
	s.toolStartSeen = nil
	usage := s.lastUsage
	s.lastUsage = nil
	s.turnMu.Unlock()

	logArgs := []any{
		"component", "opencode",
		"runID", runID,
		"finalLen", len(finalText),
		"final", truncRunes(finalText, 500),
	}
	if usage != nil {
		logArgs = append(logArgs,
			"inputTokens", usage.InputTokens,
			"outputTokens", usage.OutputTokens)
	}
	slog.Info("opencode <<< turn completed", logArgs...)

	// Clear ONLY currentRunID: pendingRunID belongs to the NEXT turn (it was
	// already consumed at ensureTurnStarted for this one) — a sendChat that
	// queued while this turn streamed must keep its runID so silent/web-chat
	// markers still match.
	s.currentRunID.Store("")

	// Telegram-originated turn (telegram_poll.go): DM the reply back to the
	// originating chat. TTS was suppressed at injection (MarkSilentRun), so
	// this DM is the user-visible output. [HW:/...] hardware markers and TTS
	// audio tags are for HAL, not the chat bubble — strip them
	// (stripForChannel, hal.go). Best-effort in a goroutine: the read loop
	// must not block on the Bot API.
	if chatID := s.consumeTelegramRun(runID); chatID != "" && finalText != "" {
		if reply := stripForChannel(finalText); reply != "" {
			go func() {
				if err := s.SendToUser(chatID, reply, ""); err != nil {
					slog.Error("telegram reply send failed",
						"component", "opencode", "runID", runID, "chatID", chatID, "error", err)
				}
			}()
		}
	}

	// Slack-originated turn (slack.go): post the reply back to the originating
	// channel/thread (chat.postMessage) and clear the eyes ack reaction. TTS
	// was suppressed at injection (MarkSilentRun); markers are stripped like
	// the telegram path. Consumed here — synchronously, before dispatch — so
	// the shared handler's DeliverSlackReply safety net stays a no-op.
	// Best-effort in a goroutine: the read loop must not block on the Web API.
	if o, ok := s.consumeSlackRun(runID); ok {
		go s.finishSlackTurn(o, stripForChannel(finalText))
	}

	// Discord-originated turn (discord.go): post the reply back to the
	// originating channel (chunked at Discord's 2000-char limit). TTS was
	// suppressed at injection (MarkSilentRun); markers are stripped like the
	// telegram path. Best-effort in a goroutine: the read loop must not block
	// on the Discord API.
	if channelID := s.consumeDiscordRun(runID); channelID != "" && finalText != "" {
		go s.finishDiscordTurn(channelID, stripForChannel(finalText))
	}

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
			"usage":   usage,
		},
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: endPayload})
}

func (s *OpenCodeService) handleError(msg string, dispatch func(domain.WSEvent)) {
	s.ensureTurnStarted(dispatch) // make sure a runID exists for the error
	runID := s.getCurrentRunID()
	if msg == "" {
		msg = "opencode error"
	}
	slog.Warn("opencode <<< error", "component", "opencode", "runID", runID, "error", msg)

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

	// Telegram-originated turn: consume the tracker so the map doesn't leak.
	// No DM — the user simply gets no reply for a failed turn.
	if chatID := s.consumeTelegramRun(runID); chatID != "" {
		slog.Warn("telegram-originated turn failed — reply dropped",
			"component", "opencode", "runID", runID, "chatID", chatID)
	}

	// Slack-originated turn: consume the tracker (no reply for a failed turn)
	// and clear the eyes ack reaction so the message isn't left marked.
	if o, ok := s.consumeSlackRun(runID); ok {
		slog.Warn("slack-originated turn failed — reply dropped",
			"component", "opencode", "runID", runID, "channel", o.channel)
		go s.finishSlackTurn(o, "")
	}

	// Discord-originated turn: consume the tracker so the map doesn't leak
	// (and the typing keeper stops). No reply for a failed turn.
	if channelID := s.consumeDiscordRun(runID); channelID != "" {
		slog.Warn("discord-originated turn failed — reply dropped",
			"component", "opencode", "runID", runID, "channelID", channelID)
	}

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

func (s *OpenCodeService) getCurrentRunID() string {
	v, _ := s.currentRunID.Load().(string)
	return v
}

func (s *OpenCodeService) setCurrentRunID(runID string) { s.currentRunID.Store(runID) }

func (s *OpenCodeService) setPendingRunID(runID string) { s.pendingRunID.Store(runID) }

func (s *OpenCodeService) consumePendingRunID() string {
	v, _ := s.pendingRunID.Load().(string)
	if v != "" {
		s.pendingRunID.Store("")
	}
	return v
}

// clearTurn resets the in-flight turn ids without touching busy state. Used on
// disconnect / busyTTL expiry / send failure. The normal end-of-turn path
// clears the ids inline in emitFinal / handleError (before dispatch).
func (s *OpenCodeService) clearTurn() {
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
