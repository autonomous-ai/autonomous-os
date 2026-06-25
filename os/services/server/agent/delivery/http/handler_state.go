package http

import (
	"log/slog"
	"strings"
)

// accumulateAssistantDelta appends a delta to the buffer for the given runId.
func (h *AgentHandler) accumulateAssistantDelta(runID, delta string) {
	if delta == "" {
		return
	}
	h.assistantMu.Lock()
	defer h.assistantMu.Unlock()
	buf, ok := h.assistantBuf[runID]
	if !ok {
		buf = &strings.Builder{}
		h.assistantBuf[runID] = buf
	}
	buf.WriteString(delta)
	slog.Info("assistant delta buffered (TTS waits for lifecycle:end)",
		"component", "agent",
		"run_id", runID,
		"delta", delta,
		"cumulative_len", buf.Len(),
		"cumulative_tail", tailPreview(buf.String(), 120),
	)
}

// tailPreview returns the last n chars of s for log readability without spamming
// the entire growing buffer on every delta.
func tailPreview(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return "…" + s[len(s)-n:]
}

// tryFirstSentenceFlush returns the FIRST complete sentence in the per-run
// buffer once it is safe to stream to TTS, or "" when no sentence is ready
// or one has already been streamed for this run. The raw buffer is left
// intact so flushAssistantText at lifecycle:end still sees every HW marker.
//
// Why only the first sentence: chaining every sentence as its own
// /voice/speak POST exposes a ~400ms ElevenLabs/OpenAI TTFB gap between
// each one — perceived as choppy. Streaming just the first sentence wins
// most of the first-audio latency (~2s) while letting the remainder go
// through /voice/speak-queue at lifecycle:end, which Python pre-synthesises
// while the first sentence is still playing.
//
// Defers (returns "") when the snapshot has:
//   - a partial `[HW:` marker (extractHWCalls only matches complete markers)
//   - any `<say>` wrapper (extractSayTag at end shifts content)
//   - `NO_REPLY` / `HEARTBEAT_OK` sentinels (sanitizeAgentText strips
//     these at end-flush; streamed text can't be unspoken)
func (h *AgentHandler) tryFirstSentenceFlush(runID string) string {
	h.assistantMu.Lock()
	defer h.assistantMu.Unlock()

	// Already streamed first sentence for this run — let lifecycle:end
	// handle the rest via /voice/speak-queue.
	if _, already := h.streamedCleanLen[runID]; already {
		return ""
	}
	buf, ok := h.assistantBuf[runID]
	if !ok || buf.Len() == 0 {
		return ""
	}
	raw := buf.String()
	if hasPartialHWMarker(raw) {
		return ""
	}
	if strings.Contains(raw, "<say>") {
		return ""
	}
	upper := strings.ToUpper(raw)
	if strings.Contains(upper, "NO_REPLY") || strings.Contains(upper, "HEARTBEAT_OK") {
		return ""
	}

	_, cleaned := extractHWCalls(raw)
	cleaned = prunedImageMarkerRe.ReplaceAllString(cleaned, "")
	cleaned = strings.TrimSpace(cleaned)
	if cleaned == "" {
		return ""
	}

	boundary := findSentenceFlushBoundary(cleaned)
	if boundary < 0 {
		return ""
	}
	sentence := strings.TrimSpace(cleaned[:boundary+1])
	if sentence == "" {
		return ""
	}
	h.streamedCleanLen[runID] = boundary + 1
	return sentence
}

// cleanedSlackStreamText returns the cleaned cumulative reply text for runID, safe
// to stream to Slack (chat.appendStream), and ready=false when streaming should
// defer this round. It mirrors tryFirstSentenceFlush's cleaning but returns the WHOLE
// text (not just the first sentence) and does not track a streamed offset — the Slack
// bridge diffs against what it has already appended. Defers (ready=false) on a partial
// `[HW:` marker, any `<say>` wrapper, or a NO_REPLY / HEARTBEAT_OK sentinel, so junk
// never reaches the channel mid-stream.
func (h *AgentHandler) cleanedSlackStreamText(runID string) (string, bool) {
	h.assistantMu.Lock()
	buf, ok := h.assistantBuf[runID]
	var raw string
	if ok && buf != nil {
		raw = buf.String()
	}
	h.assistantMu.Unlock()
	if raw == "" {
		return "", false
	}
	if hasPartialHWMarker(raw) || strings.Contains(raw, "<say>") {
		return "", false
	}
	upper := strings.ToUpper(raw)
	if strings.Contains(upper, "NO_REPLY") || strings.Contains(upper, "HEARTBEAT_OK") {
		return "", false
	}
	_, cleaned := extractHWCalls(raw)
	cleaned = prunedImageMarkerRe.ReplaceAllString(cleaned, "")
	cleaned = strings.TrimSpace(cleaned)
	if cleaned == "" {
		return "", false
	}
	return cleaned, true
}

// consumeStreamedCleanLen returns the byte offset into the cleaned reply
// already streamed to TTS for runID and clears the entry. Called at
// lifecycle:end so the remainder POST sends only what was not already
// streamed. Returns 0 when no sentence was streamed for this run.
func (h *AgentHandler) consumeStreamedCleanLen(runID string) int {
	h.assistantMu.Lock()
	defer h.assistantMu.Unlock()
	n, ok := h.streamedCleanLen[runID]
	if !ok {
		return 0
	}
	delete(h.streamedCleanLen, runID)
	return n
}

// ADDED 2026-05-26: firedHWCount helpers — track leading HW markers fired at
// stream-time so lifecycle:end can skip them (avoid double-fire).
//
// readFiredHWCount returns the current fired count for runID (0 if none).
// Does NOT delete the entry — stream-time may fire more markers as buf grows.
func (h *AgentHandler) readFiredHWCount(runID string) int {
	h.assistantMu.Lock()
	defer h.assistantMu.Unlock()
	return h.firedHWCount[runID]
}

// recordFiredHWCount sets the fired count for runID, called after stream-time
// fires markers so lifecycle:end knows where to skip from.
func (h *AgentHandler) recordFiredHWCount(runID string, count int) {
	h.assistantMu.Lock()
	defer h.assistantMu.Unlock()
	h.firedHWCount[runID] = count
}

// consumeFiredHWCount returns the fired count and deletes the entry. Called
// at lifecycle:end (and channel-turn finalize) after the dedupe slice is taken.
func (h *AgentHandler) consumeFiredHWCount(runID string) int {
	h.assistantMu.Lock()
	defer h.assistantMu.Unlock()
	n := h.firedHWCount[runID]
	delete(h.firedHWCount, runID)
	return n
}

// hasPartialHWMarker reports whether text contains a `[HW:` opener with no
// matching `]` before EOF. extractHWCalls only matches complete markers, so
// a partial marker would survive into cleaned text and could be split
// mid-sentence. tryFirstSentenceFlush defers in that case.
func hasPartialHWMarker(text string) bool {
	idx := strings.Index(text, "[HW:")
	for idx >= 0 {
		end := strings.Index(text[idx:], "]")
		if end < 0 {
			return true
		}
		next := strings.Index(text[idx+4:], "[HW:")
		if next < 0 {
			return false
		}
		idx = idx + 4 + next
	}
	return false
}

// findSentenceFlushBoundary returns the rightmost index in s of `[.?!]`
// followed by whitespace, or -1 if none. The trailing-whitespace requirement
// confirms the next token has begun (so we're not splitting an abbreviation
// or version number mid-formation). Decimal patterns "5. 5" are also skipped.
func findSentenceFlushBoundary(s string) int {
	n := len(s)
	for i := n - 2; i >= 0; i-- {
		c := s[i]
		if c != '.' && c != '?' && c != '!' {
			continue
		}
		next := s[i+1]
		if next != ' ' && next != '\n' && next != '\t' && next != '\r' {
			continue
		}
		if i > 0 && isAsciiDigit(s[i-1]) {
			j := i + 1
			for j < n && (s[j] == ' ' || s[j] == '\t') {
				j++
			}
			if j < n && isAsciiDigit(s[j]) {
				continue
			}
		}
		return i
	}
	return -1
}

func isAsciiDigit(b byte) bool {
	return b >= '0' && b <= '9'
}

// flushAssistantText returns the accumulated text for runId and clears the buffer.
// HW markers are stripped here so they never appear in Telegram or other channel replies.
// The caller is responsible for extracting and firing HW calls before flushing.
func (h *AgentHandler) flushAssistantText(runID string) (string, []hwCall) {
	h.assistantMu.Lock()
	defer h.assistantMu.Unlock()
	buf, ok := h.assistantBuf[runID]
	if !ok || buf.Len() == 0 {
		return "", nil
	}
	raw := buf.String()
	raw = prunedImageMarkerRe.ReplaceAllString(raw, "")
	calls, text := extractHWCalls(raw)
	text = strings.TrimSpace(text)
	delete(h.assistantBuf, runID)
	return text, calls
}

// recordAssistantDelta increments streaming counters for runID and reports
// whether this delta is the first one seen for the run. Caller emits
// agent_first_token when isFirst==true.
func (h *AgentHandler) recordAssistantDelta(runID, delta string) (isFirst bool) {
	if delta == "" {
		return false
	}
	h.streamStatsMu.Lock()
	defer h.streamStatsMu.Unlock()
	s, ok := h.streamStats[runID]
	if !ok {
		s = &runStreamStats{}
		h.streamStats[runID] = s
	}
	isFirst = !s.assistantFirstSeen
	s.assistantFirstSeen = true
	s.assistantChunks++
	s.assistantChars += len(delta)
	s.assistantText.WriteString(delta)
	return isFirst
}

// recordThinkingDelta is the thinking counterpart. Thinking text is
// accumulated here because there is no separate per-run thinking buffer.
func (h *AgentHandler) recordThinkingDelta(runID, delta string) (isFirst bool) {
	if delta == "" {
		return false
	}
	h.streamStatsMu.Lock()
	defer h.streamStatsMu.Unlock()
	s, ok := h.streamStats[runID]
	if !ok {
		s = &runStreamStats{}
		h.streamStats[runID] = s
	}
	isFirst = !s.thinkingFirstSeen
	s.thinkingFirstSeen = true
	s.thinkingChunks++
	s.thinkingChars += len(delta)
	s.thinkingText.WriteString(delta)
	return isFirst
}

// drainStreamStats returns the stats snapshot for runID and clears it.
// Returns nil when no streaming was recorded for the run.
func (h *AgentHandler) drainStreamStats(runID string) *runStreamStats {
	h.streamStatsMu.Lock()
	defer h.streamStatsMu.Unlock()
	s, ok := h.streamStats[runID]
	if !ok {
		return nil
	}
	delete(h.streamStats, runID)
	return s
}

// suppressTTS flags a runID to skip TTS on lifecycle end with the given reason.
func (h *AgentHandler) suppressTTS(runID, reason string) {
	h.ttsSuppressMu.Lock()
	defer h.ttsSuppressMu.Unlock()
	// "music_playing" takes priority over "already_spoken" (speaker conflict is more important).
	if existing := h.ttsSuppressReasons[runID]; existing == "music_playing" && reason != "music_playing" {
		return
	}
	h.ttsSuppressReasons[runID] = reason
}

// clearTTSSuppress removes the suppress flag for a runID and returns the reason (empty if none).
func (h *AgentHandler) clearTTSSuppress(runID string) string {
	h.ttsSuppressMu.Lock()
	defer h.ttsSuppressMu.Unlock()
	reason := h.ttsSuppressReasons[runID]
	delete(h.ttsSuppressReasons, runID)
	return reason
}

// resolveRunID maps an OpenClaw-assigned UUID back to the device idempotencyKey if known.
// If no mapping exists, returns the original runID unchanged.
func (h *AgentHandler) resolveRunID(runID string) string {
	h.runIDMapMu.Lock()
	defer h.runIDMapMu.Unlock()
	if mapped, ok := h.runIDMap[runID]; ok {
		return mapped
	}
	return runID
}

// mapRunID records that OpenClaw UUID corresponds to the given device trace (idempotencyKey).
func (h *AgentHandler) mapRunID(openclawID, deviceID string) {
	h.runIDMapMu.Lock()
	defer h.runIDMapMu.Unlock()
	h.runIDMap[openclawID] = deviceID
	// Limit map size to prevent unbounded growth
	if len(h.runIDMap) > 200 {
		for k := range h.runIDMap {
			delete(h.runIDMap, k)
			break
		}
	}
}
