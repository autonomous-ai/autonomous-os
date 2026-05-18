package sse

import (
	"log/slog"
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

// trySentenceFlush atomically checks the per-run buffer for a completed
// sentence that is safe to dispatch to TTS mid-turn (sentence-boundary
// streaming). When found, moves the sentence text out of assistantBuf into
// streamedSentences and returns it so the caller can fire the TTS POST; the
// remainder stays in assistantBuf and is flushed at lifecycle:end. Returns
// "" when no sentence boundary is present or the buffer contains content
// that must be handled at end-of-turn (HW markers, <say> wrapper, NO_REPLY /
// HEARTBEAT_OK sentinels).
func (h *OpenClawHandler) trySentenceFlush(runID string) string {
	h.assistantMu.Lock()
	defer h.assistantMu.Unlock()

	buf, ok := h.assistantBuf[runID]
	if !ok || buf.Len() == 0 {
		return ""
	}
	text := buf.String()
	if !safeToStreamFlush(text) {
		return ""
	}
	boundary := findSentenceFlushBoundary(text)
	if boundary < 0 {
		return ""
	}
	sentence := strings.TrimSpace(text[:boundary+1])
	remainder := text[boundary+1:]
	if sentence == "" {
		return ""
	}

	buf.Reset()
	buf.WriteString(remainder)

	streamed, ok := h.streamedSentences[runID]
	if !ok {
		streamed = &strings.Builder{}
		h.streamedSentences[runID] = streamed
	}
	if streamed.Len() > 0 {
		streamed.WriteByte(' ')
	}
	streamed.WriteString(sentence)

	return sentence
}

// consumeStreamedSentences returns the cumulative sentence-streamed text for
// runID and clears the entry. Called at lifecycle:end so broadcast/DM fan-out
// can reconstruct the full spoken reply (streamed + remainder). Returns ""
// when sentence streaming did not fire for this run.
func (h *OpenClawHandler) consumeStreamedSentences(runID string) string {
	h.assistantMu.Lock()
	defer h.assistantMu.Unlock()
	streamed, ok := h.streamedSentences[runID]
	if !ok {
		return ""
	}
	delete(h.streamedSentences, runID)
	return streamed.String()
}

// safeToStreamFlush returns true when text contains no markers or sentinels
// that require end-of-turn handling. HW markers must reach extractHWCalls to
// fire hardware tools; <say> wrappers must reach extractSayTag so reasoning
// outside the tag never leaks to TTS; NO_REPLY / HEARTBEAT_OK sentinels are
// stripped by sanitizeAgentText at end-flush.
func safeToStreamFlush(text string) bool {
	if strings.Contains(text, "[HW:") {
		return false
	}
	if strings.Contains(text, "<say>") {
		return false
	}
	upper := strings.ToUpper(text)
	if strings.Contains(upper, "NO_REPLY") || strings.Contains(upper, "HEARTBEAT_OK") {
		return false
	}
	return true
}

// findSentenceFlushBoundary returns the index in s of the rightmost sentence-
// terminating punctuation ([.?!]) followed by whitespace, or -1 if none. The
// trailing-whitespace requirement is what makes the boundary safe to flush:
// it confirms the next token has already begun (so we're not splitting an
// abbreviation or version number mid-formation). Decimal patterns like "5. 5"
// — digit before, digit after the whitespace run — are still skipped as a
// safety net.
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
