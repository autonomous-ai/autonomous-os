package sse

import (
	"strings"
)

// accumulateAssistantDelta appends a delta to the buffer for the given runId.
func (h *OpenClawHandler) accumulateAssistantDelta(runID, delta string) {
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
}

// flushAssistantText returns the accumulated text for runId and clears the buffer.
// HW markers are stripped here so they never appear in Telegram or other channel replies.
// The caller is responsible for extracting and firing HW calls before flushing.
func (h *OpenClawHandler) flushAssistantText(runID string) (string, []hwCall) {
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
func (h *OpenClawHandler) recordAssistantDelta(runID, delta string) (isFirst bool) {
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
func (h *OpenClawHandler) recordThinkingDelta(runID, delta string) (isFirst bool) {
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
func (h *OpenClawHandler) drainStreamStats(runID string) *runStreamStats {
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
func (h *OpenClawHandler) suppressTTS(runID, reason string) {
	h.ttsSuppressMu.Lock()
	defer h.ttsSuppressMu.Unlock()
	// "music_playing" takes priority over "already_spoken" (speaker conflict is more important).
	if existing := h.ttsSuppressReasons[runID]; existing == "music_playing" && reason != "music_playing" {
		return
	}
	h.ttsSuppressReasons[runID] = reason
}

// clearTTSSuppress removes the suppress flag for a runID and returns the reason (empty if none).
func (h *OpenClawHandler) clearTTSSuppress(runID string) string {
	h.ttsSuppressMu.Lock()
	defer h.ttsSuppressMu.Unlock()
	reason := h.ttsSuppressReasons[runID]
	delete(h.ttsSuppressReasons, runID)
	return reason
}

// resolveRunID maps an OpenClaw-assigned UUID back to the device idempotencyKey if known.
// If no mapping exists, returns the original runID unchanged.
func (h *OpenClawHandler) resolveRunID(runID string) string {
	h.runIDMapMu.Lock()
	defer h.runIDMapMu.Unlock()
	if mapped, ok := h.runIDMap[runID]; ok {
		return mapped
	}
	return runID
}

// mapRunID records that OpenClaw UUID corresponds to the given device trace (idempotencyKey).
func (h *OpenClawHandler) mapRunID(openclawID, deviceID string) {
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
