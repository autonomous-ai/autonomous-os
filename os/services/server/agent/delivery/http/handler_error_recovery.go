package http

import (
	"encoding/json"
	"log/slog"
	"strings"
	"time"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/lib/flow"
)

// Incomplete-turn error recovery.
//
// OpenClaw 2026.6.x ships an "incomplete turn detected" check that surfaces
// "⚠️ Agent couldn't generate a response" when its end-of-turn payload count
// is 0 — even when the model DID reply (text arrived via streamed deltas, or
// only landed in session history; openclaw#68076 / #67855 family, worst in
// 2026.6.11 #98528). Without recovery the web UI renders an error banner and
// the spoken reply is lost while the tools' side effects already ran.
//
// tryRecoverIncompleteTurn salvages the reply on lifecycle:error:
//  1. streamed deltas buffered for this run (authoritative), else
//  2. chat.history — but ONLY assistant messages that appear AFTER the last
//     user message, so a turn that truly produced nothing can never re-show
//     the previous turn's reply.
//
// Recovered turns emit the same flow events as the normal lifecycle:end path
// (tts_send / tts_suppressed + chat_response) so web chat and Flow Monitor
// render a completed turn; markers from a history-recovered text are stripped
// but NOT fired (the turn's tool side effects already executed).

// errorRecoveryTTL is how long a recovered run suppresses follow-up error
// banners. The gateway auto-retries an incomplete turn and re-surfaces the
// same error ~15s later on the chat stream; 2 minutes covers retries without
// masking a genuinely new failure of a later turn (runIDs are unique anyway).
const errorRecoveryTTL = 2 * time.Minute

// tryRecoverIncompleteTurn attempts to salvage the assistant reply for an
// errored run. Returns true when a reply was recovered and emitted — the
// caller then renders the lifecycle event as recovered and later chat-stream
// errors for this run are suppressed via markErrorRecovered/wasErrorRecovered.
func (h *AgentHandler) tryRecoverIncompleteTurn(runID, flowRunID, sessionKey string) bool {
	// Scope: device-originated runs and the agent's main session only.
	// Sub-session lifecycles (subagents etc.) have no device-facing reply.
	if !isDeviceOutboundChatRunID(flowRunID) && sessionKey != h.agentGateway.GetSessionKey() {
		return false
	}

	// Persist streaming counters exactly like lifecycle:end would — the flow
	// monitor otherwise shows no assistant row for the recovered turn.
	if s := h.drainStreamStats(flowRunID); s != nil && s.assistantChunks > 0 {
		flow.Log("agent_last_token", map[string]any{
			"run_id": flowRunID,
			"text":   s.assistantText.String(),
			"chunks": s.assistantChunks,
			"chars":  s.assistantChars,
		}, flowRunID)
	}

	text, hwCalls := h.flushAssistantText(runID)
	source := "stream_buffer"
	if strings.TrimSpace(text) == "" {
		hist, err := h.agentGateway.FetchChatHistory(sessionKey, 10)
		if err != nil || hist == nil {
			return false
		}
		raw := extractTrailingAssistantFromHistory(hist)
		if strings.TrimSpace(raw) == "" {
			return false
		}
		// Strip markers but never fire them from history text: the turn's
		// tool/marker side effects already ran inside the gateway.
		_, text = extractHWCalls(raw)
		hwCalls = nil
		source = "chat_history"
	}

	// Sentinels mean nothing user-visible was lost — let the error stand.
	if strings.Contains(strings.ToUpper(text), "HEARTBEAT_OK") {
		return false
	}
	text = extractSayTag(text)
	text = sanitizeAgentText(text)
	if strings.TrimSpace(text) == "" || isAgentNoReply(text) {
		return false
	}

	// CoT-leak filter parity with the lifecycle:end flush.
	f := newCoTLeakFilter(h.replyLanguageCode())
	filtered := strings.TrimSpace(f.filterText(text))
	if len(f.dropped) > 0 {
		flow.Log("cot_leak_filtered", map[string]any{
			"run_id":  flowRunID,
			"dropped": len(f.dropped),
			"preview": cotDroppedPreview(f.dropped, 500),
		}, flowRunID)
	}
	if filtered == "" {
		return false
	}
	text = filtered

	// Buffer-recovered markers: fire the ones not already fired at stream
	// time (leading markers fire mid-turn via tryFirstSentenceFlush).
	fired := h.consumeFiredHWCount(runID)
	if fired > len(hwCalls) {
		fired = len(hwCalls)
	}
	if rest := hwCalls[fired:]; len(rest) > 0 {
		h.fireHWCallsSync(rest, flowRunID)
	}

	// Same suppress ladder as lifecycle:end (consuming the flags here is
	// correct — no lifecycle:end will follow for this run).
	suppress := h.clearTTSSuppress(runID)
	if suppress == "" && h.agentGateway.ConsumeWebChatRun(flowRunID) {
		suppress = "web_chat"
	}
	if suppress == "" && h.agentGateway.ConsumeSilentRun(flowRunID) {
		suppress = "voice_agent_handled"
	}
	if suppress == "" && isChannelOriginatedRun(runID, flowRunID) {
		suppress = "channel_run"
	}

	streamedLen := h.consumeStreamedCleanLen(runID)
	if streamedLen > len(text) {
		streamedLen = len(text)
	}
	remainder := strings.TrimSpace(text[streamedLen:])

	slog.Warn("recovered assistant reply from errored turn",
		"component", "agent", "run_id", flowRunID, "source", source,
		"chars", len(text), "suppress", suppress)
	flow.Log("agent_error_recovered", map[string]any{
		"run_id": flowRunID,
		"source": source,
		"chars":  len(text),
	}, flowRunID)
	h.monitorBus.Push(domain.MonitorEvent{
		Type:    "chat_response",
		Summary: text[:min(len(text), 120)],
		RunID:   flowRunID,
		State:   "final",
		Detail:  map[string]string{"role": "assistant", "message": text, "recovered": "true"},
	})

	switch {
	case suppress != "":
		flow.Log("tts_suppressed", map[string]any{"run_id": flowRunID, "reason": suppress, "text": text}, flowRunID)
	case remainder != "":
		// full_text carries the whole reply for web display (chat + flow
		// turn read tts_send only); remainder skips the already-streamed
		// first sentence exactly like the lifecycle:end path.
		flow.Log("tts_send", map[string]any{"run_id": flowRunID, "text": remainder, "full_text": text, "streamed_len": streamedLen}, flowRunID)
		go func(t string) {
			if err := h.agentGateway.SendToHALTTSQueue(t); err != nil {
				slog.Error("recovered-reply TTS delivery failed", "component", "agent", "error", err)
			}
		}(remainder)
	default:
		// Single-sentence reply already fully streamed mid-turn.
		flow.Log("tts_stream_complete", map[string]any{"run_id": flowRunID, "text": text}, flowRunID)
	}
	return true
}

// extractTrailingAssistantFromHistory returns the joined text of assistant
// messages that appear AFTER the last user message in a chat.history payload,
// or "" when the window has no user anchor or nothing follows it. The anchor
// requirement is the guard against resurfacing the previous turn's reply.
func extractTrailingAssistantFromHistory(payload json.RawMessage) string {
	var hist struct {
		Messages []struct {
			Role    string          `json:"role"`
			Content json.RawMessage `json:"content"`
		} `json:"messages"`
	}
	if json.Unmarshal(payload, &hist) != nil {
		return ""
	}
	lastUser := -1
	for i, m := range hist.Messages {
		if m.Role == "user" {
			lastUser = i
		}
	}
	if lastUser < 0 {
		return ""
	}
	var parts []string
	for _, m := range hist.Messages[lastUser+1:] {
		if m.Role != "assistant" {
			continue
		}
		if t := historyContentText(m.Content); strings.TrimSpace(t) != "" {
			parts = append(parts, t)
		}
	}
	return strings.TrimSpace(strings.Join(parts, "\n"))
}

// historyContentText flattens a chat.history message content — plain string
// or an array of {type,text} blocks (same shapes as
// extractLastUserMessageFromHistory handles).
func historyContentText(content json.RawMessage) string {
	var s string
	if json.Unmarshal(content, &s) == nil {
		return s
	}
	var blocks []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	if json.Unmarshal(content, &blocks) == nil {
		var parts []string
		for _, b := range blocks {
			if b.Type == "text" && strings.TrimSpace(b.Text) != "" {
				parts = append(parts, b.Text)
			}
		}
		return strings.Join(parts, " ")
	}
	return ""
}

// markErrorRecovered records that flowRunID's reply was recovered so the
// chat-stream error (and the gateway's retry error ~15s later) can be
// suppressed instead of rendering a banner over the recovered reply.
func (h *AgentHandler) markErrorRecovered(flowRunID string) {
	now := time.Now()
	h.errorRecoveredMu.Lock()
	defer h.errorRecoveredMu.Unlock()
	for id, t := range h.errorRecoveredRuns {
		if now.Sub(t) > errorRecoveryTTL {
			delete(h.errorRecoveredRuns, id)
		}
	}
	h.errorRecoveredRuns[flowRunID] = now
}

// wasErrorRecovered reports whether flowRunID had its reply recovered within
// errorRecoveryTTL.
func (h *AgentHandler) wasErrorRecovered(flowRunID string) bool {
	h.errorRecoveredMu.Lock()
	defer h.errorRecoveredMu.Unlock()
	t, ok := h.errorRecoveredRuns[flowRunID]
	return ok && time.Since(t) <= errorRecoveryTTL
}
