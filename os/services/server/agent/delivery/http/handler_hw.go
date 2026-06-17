package http

import (
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"regexp"
	"strings"
	"time"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/internal/device"
	"go.autonomous.ai/os/lib/flow"
	"go.autonomous.ai/os/lib/hal"
	"go.autonomous.ai/os/lib/i18n"
)

// trackFailMessage is the apology template spoken when /servo/track fails.
// Template (with %s for the target name) lives in lib/i18n
// (PhraseTrackFailFmt). Soft ask-for-help phrasing per SOUL.md rather
// than an assistant-style option dispatch.
func trackFailMessage(target string) string {
	return fmt.Sprintf(i18n.One(i18n.PhraseTrackFailFmt), target)
}

// parseTrackTarget pulls the first candidate label out of a
// [HW:/servo/track:{"target":...}] body. Accepts string or []string forms.
// Returns "it" if parsing fails so fallback TTS still reads naturally.
func parseTrackTarget(body string) string {
	var req struct {
		Target any `json:"target"`
	}
	if err := json.Unmarshal([]byte(body), &req); err != nil {
		return "it"
	}
	switch v := req.Target.(type) {
	case string:
		if v != "" {
			return v
		}
	case []any:
		for _, t := range v {
			if s, ok := t.(string); ok && s != "" {
				return s
			}
		}
	}
	return "it"
}

// prunedImageMarkerRe matches bracket markers echoed by the LLM after OpenClaw
// strips image payloads from conversation history (e.g. "[image description removed]").
var prunedImageMarkerRe = regexp.MustCompile(`\[image[^\]]*removed[^\]]*\]`)

// hwMarkerRe matches inline hardware markers like [HW:/emotion:{"emotion":"happy","intensity":0.9}]
// JSON body must not contain '}' except as the final closing brace (no nested objects).
// The body is OPTIONAL: bodyless markers like [HW:/led/off] are valid — several HAL
// endpoints (e.g. /led/off, /led/effect/stop) take no body, and the LLM naturally
// omits the empty `{}` for them. Without the optional group, [HW:/led/off] failed to
// match and the command was silently dropped (the LED never turned off) while
// [HW:/led/solid:{...}] worked. extractHWCalls defaults a missing body to `{}`.
var hwMarkerRe = regexp.MustCompile(`\[HW:(/[^:]+)(?::(\{[^}]*\}))?\]`)

type hwCall struct {
	path string
	body string
}

// extractHWCalls parses all [HW:/path:{"json"}] markers from text,
// returns the list of calls and the text with all markers stripped.
func extractHWCalls(text string) ([]hwCall, string) {
	matches := hwMarkerRe.FindAllStringSubmatch(text, -1)
	calls := make([]hwCall, 0, len(matches))
	for _, m := range matches {
		body := m[2]
		if body == "" { // bodyless marker like [HW:/led/off] → default to empty JSON
			body = "{}"
		}
		calls = append(calls, hwCall{path: m[1], body: body})
	}
	// Log only when buddy markers are present — keeps signal-to-noise high
	// while answering the "did OpenClaw fire a /buddy/* marker?" question.
	hasBuddy := false
	paths := make([]string, 0, len(calls))
	for _, c := range calls {
		paths = append(paths, c.path)
		if strings.HasPrefix(c.path, "/buddy/") {
			hasBuddy = true
		}
	}
	if hasBuddy {
		slog.Info("HW markers extracted", "component", "agent-hw", "count", len(calls), "paths", paths)
	}
	return calls, strings.TrimSpace(hwMarkerRe.ReplaceAllString(text, ""))
}

// ADDED 2026-05-26: extractLeadingHWCalls returns markers that appear BEFORE
// the first non-marker, non-whitespace text. Used at stream-time to fire only
// markers preceding the streaming sentence; inline markers (between sentences)
// stay deferred to lifecycle:end to preserve position-in-text semantics.
// Caller hands the resulting count to recordFiredHWCount so lifecycle:end can
// skip these markers via extractHWCalls's stable in-order output.
func extractLeadingHWCalls(text string) []hwCall {
	matches := hwMarkerRe.FindAllStringSubmatchIndex(text, -1)
	var calls []hwCall
	expectedPos := 0
	for _, m := range matches {
		start, end := m[0], m[1]
		// Only whitespace allowed between previous end and this marker's start.
		for expectedPos < start {
			c := text[expectedPos]
			if c != ' ' && c != '\n' && c != '\t' && c != '\r' {
				return calls
			}
			expectedPos++
		}
		// Body group (m[4]:m[5]) is optional — it's -1 for a bodyless marker
		// like [HW:/led/off]; default that to empty JSON (text[-1:] would panic).
		body := "{}"
		if m[4] >= 0 {
			body = text[m[4]:m[5]]
		}
		calls = append(calls, hwCall{path: text[m[2]:m[3]], body: body})
		expectedPos = end
	}
	return calls
}

// ADDED 2026-05-26: fireHWCall runs the per-marker POST + flow tracking +
// monitorBus emit + lastEmotion update for one call. Extracted from
// fireHWCalls so both async (default 5s timeout) and sync (100ms stream-time)
// paths share identical side-effects. Returns true on HTTP success (2xx/3xx).
func (h *AgentHandler) fireHWCall(c hwCall, flowRunID string, client *http.Client) bool {
	// /broadcast, /speak, /dm are internal control markers — not HAL endpoints.
	if c.path == "/broadcast" || c.path == "/speak" || c.path == "/dm" {
		return true
	}
	// The OS — not the brain — is the deterministic gate over the body. Drop a
	// HW marker the agent emitted for hardware this device doesn't declare, so a
	// swappable/hallucinating brain (or an over-eager SOUL) can never drive a
	// peripheral that isn't there. Skill-gating already keeps the LLM from
	// knowing about absent hardware; this enforces it regardless of the brain.
	if cap := device.RouteCapability(c.path); cap != "" && !device.Has(h.config.DeviceTypeOrDefault(), cap) {
		slog.Debug("HW marker dropped — device lacks capability",
			"component", "openclaw", "path", c.path, "capability", cap)
		return true
	}
	// Lamp-bound HW markers (log writes that live on Lamp, not HAL):
	//   /wellbeing/log         → POST :5000/api/wellbeing/log
	//   /mood/log              → POST :5000/api/mood/log (signal + decision share endpoint, kind in body)
	//   /music-suggestion/log  → POST :5000/api/music-suggestion/log
	//   /posture/log           → POST :5000/api/posture/log (nudge/praise/recap rows)
	postURL := hal.BaseURL + c.path
	if strings.HasPrefix(c.path, "/wellbeing/") ||
		strings.HasPrefix(c.path, "/mood/") ||
		strings.HasPrefix(c.path, "/music-suggestion/") ||
		strings.HasPrefix(c.path, "/posture/") ||
		strings.HasPrefix(c.path, "/buddy/") {
		postURL = "http://127.0.0.1:5000/api" + c.path
	}
	isBuddy := strings.HasPrefix(c.path, "/buddy/")
	if isBuddy {
		slog.Info("HW marker → buddy POST", "component", "openclaw", "url", postURL, "body", c.body)
	}
	resp, err := client.Post(postURL, "application/json", strings.NewReader(c.body))
	if err != nil {
		slog.Warn("HW marker call failed", "component", "openclaw", "path", c.path, "error", err)
		return false
	}
	hwOK := resp.StatusCode < 400
	if !hwOK {
		errBody, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		resp.Body.Close()
		slog.Warn("HW marker error response", "component", "openclaw", "path", c.path, "status", resp.StatusCode, "body", string(errBody))

		// /servo/track start has ~800ms latency (freeze + YOLO). By
		// the time this goroutine sees the 400, the LLM's optimistic
		// TTS ("Following the cup") has already played. Speak a
		// corrective apology so the user doesn't think tracking
		// succeeded. Only for the exact start path — /stop and
		// /update have different semantics.
		if c.path == "/servo/track" {
			target := parseTrackTarget(c.body)
			if err := hal.Speak(trackFailMessage(target)); err != nil {
				slog.Warn("track fallback TTS failed", "component", "openclaw", "error", err)
			}
		}
	} else {
		if isBuddy {
			okBody, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
			resp.Body.Close()
			slog.Info("HW marker → buddy OK", "component", "openclaw", "path", c.path, "response", string(okBody))
		} else {
			resp.Body.Close()
			slog.Info("HW marker fired", "component", "openclaw", "path", c.path)
		}
	}
	switch {
	case strings.Contains(c.path, "/emotion"):
		flow.Log("hw_emotion", map[string]any{"path": c.path, "args": c.body, "run_id": flowRunID}, flowRunID)
		if hwOK {
			if e := parseEmotion(c.body); e != "" {
				h.lastEmotionMu.Lock()
				h.lastEmotion = e
				h.lastEmotionMu.Unlock()
			}
		}
		h.monitorBus.Push(domain.MonitorEvent{Type: "hw_emotion", Summary: c.path + " " + c.body, RunID: flowRunID})
	case strings.Contains(c.path, "/scene"), strings.Contains(c.path, "/led"):
		flow.Log("hw_led", map[string]any{"path": c.path, "args": c.body, "run_id": flowRunID}, flowRunID)
		h.monitorBus.Push(domain.MonitorEvent{Type: "hw_led", Summary: c.path + " " + c.body, RunID: flowRunID})
	case strings.Contains(c.path, "/servo"):
		flow.Log("hw_servo", map[string]any{"path": c.path, "args": c.body, "run_id": flowRunID}, flowRunID)
		h.monitorBus.Push(domain.MonitorEvent{Type: "hw_servo", Summary: c.path + " " + c.body, RunID: flowRunID})
	case strings.Contains(c.path, "/audio"):
		flow.Log("hw_audio", map[string]any{"path": c.path, "args": c.body, "run_id": flowRunID}, flowRunID)
		h.monitorBus.Push(domain.MonitorEvent{Type: "hw_audio", Summary: c.path + " " + c.body, RunID: flowRunID})
	case strings.HasPrefix(c.path, "/wellbeing/"):
		flow.Log("hw_wellbeing", map[string]any{"path": c.path, "args": c.body, "run_id": flowRunID}, flowRunID)
		h.monitorBus.Push(domain.MonitorEvent{Type: "hw_wellbeing", Summary: c.path + " " + c.body, RunID: flowRunID})
	case strings.HasPrefix(c.path, "/mood/"):
		flow.Log("hw_mood", map[string]any{"path": c.path, "args": c.body, "run_id": flowRunID}, flowRunID)
		h.monitorBus.Push(domain.MonitorEvent{Type: "hw_mood", Summary: c.path + " " + c.body, RunID: flowRunID})
	case strings.HasPrefix(c.path, "/music-suggestion/"):
		flow.Log("hw_music_suggestion", map[string]any{"path": c.path, "args": c.body, "run_id": flowRunID}, flowRunID)
		h.monitorBus.Push(domain.MonitorEvent{Type: "hw_music_suggestion", Summary: c.path + " " + c.body, RunID: flowRunID})
	case strings.HasPrefix(c.path, "/posture/"):
		flow.Log("hw_posture", map[string]any{"path": c.path, "args": c.body, "run_id": flowRunID}, flowRunID)
		h.monitorBus.Push(domain.MonitorEvent{Type: "hw_posture", Summary: c.path + " " + c.body, RunID: flowRunID})
	case strings.HasPrefix(c.path, "/buddy/"):
		flow.Log("hw_buddy", map[string]any{"path": c.path, "args": c.body, "run_id": flowRunID}, flowRunID)
		h.monitorBus.Push(domain.MonitorEvent{Type: "hw_buddy", Summary: c.path + " " + c.body, RunID: flowRunID})
	default:
		flow.Log("hw_call", map[string]any{"path": c.path, "args": c.body, "run_id": flowRunID}, flowRunID)
	}
	return hwOK
}

// fireHWCalls fires hardware calls to HAL sequentially in a goroutine,
// with full flow tracking, lastEmotion update, and monitorBus events.
// Sequential order matters (e.g. emotion sequences must fire in order).
func (h *AgentHandler) fireHWCalls(calls []hwCall, flowRunID string) {
	if len(calls) == 0 {
		return
	}
	go func() {
		client := &http.Client{Timeout: 5 * time.Second}
		for _, c := range calls {
			h.fireHWCall(c, flowRunID, client)
		}
	}()
}

// ADDED 2026-05-26: fireHWCallsSync fires markers SYNCHRONOUSLY with a tight
// per-call timeout. Used at stream-time when subsequent TTS depends on HAL
// state mutations (e.g. /scene/off must unmute speaker before /voice/speak).
// On per-call failure/timeout, kicks an async fallback for that one call so
// it still fires eventually — caller proceeds to TTS regardless, so worst
// case degrades to legacy "marker fires async after TTS" behavior (no new
// failure mode introduced).
func (h *AgentHandler) fireHWCallsSync(calls []hwCall, flowRunID string) {
	if len(calls) == 0 {
		return
	}
	client := &http.Client{Timeout: 100 * time.Millisecond}
	for _, c := range calls {
		if !h.fireHWCall(c, flowRunID, client) {
			// HTTP error or timeout — kick async fallback so the marker still
			// fires (legacy behavior). Caller proceeds to TTS without waiting.
			h.fireHWCalls([]hwCall{c}, flowRunID)
		}
	}
}
