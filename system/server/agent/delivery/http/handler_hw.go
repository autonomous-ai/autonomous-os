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

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/i18n"
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
//
// The path group allows colon-separated segments (e.g. /audio:play) that some LLM
// backends emit instead of the canonical slash form (/audio/play). extractHWCalls
// normalizes these to slashes after capture.
var hwMarkerRe = regexp.MustCompile(`\[HW:((?:/[^{:\]]+(?::[^{:\]]+)*))(?::(\{[^}]*\}))?\]`)

// hwLinkRe matches markdown-link-form markers like
// [Lights off right away!](HW:/led/off:{}) that markdown-trained LLMs sometimes
// emit instead of the canonical [HW:/led/off:{}] — the sentence becomes a link
// label and the marker moves into the URL slot. hwMarkerRe never matches that
// shape, so the command was silently dropped (the LED never turned off) and
// the raw link leaked into chat/TTS. normalizeHWMarkers rewrites it to the
// canonical form, keeping the label as speakable text.
//
// Tolerances beyond the canonical grammar, all observed/plausible LLM
// mangling: case-insensitive scheme (`hw:`), whitespace after `HW:`, and a
// dangling `:` with no body (`(HW:/led/off:)`). The strip-only mirrors
// (claudecode/codex stripForChannel, web stripHWMarkers, HAL strip_rt_markers)
// MUST stay exactly as loose as this regex — a variant the executor rejects
// must stay visible as raw text, never be silently scrubbed into a
// confident-looking label.
var hwLinkRe = regexp.MustCompile(`(?i)\[([^\]]*)\]\(\s*HW:\s*((?:/[^(){:\s]+(?::[^(){:\s]+)*))(?::(\{[^}]*\}))?:?\s*\)`)

// normalizeHWMarkers rewrites markdown-link-form markers to the canonical
// inline form. The marker is placed BEFORE the label so a reply that opens
// with a link still counts as a leading marker (fires at stream time).
func normalizeHWMarkers(text string) string {
	return hwLinkRe.ReplaceAllStringFunc(text, func(m string) string {
		sub := hwLinkRe.FindStringSubmatch(m)
		label, path, body := sub[1], sub[2], sub[3]
		if body == "" {
			body = "{}"
		}
		marker := "[HW:" + path + ":" + body + "]"
		// The label group can capture a preceding CANONICAL marker's content:
		// in `[HW:/led/effect/stop:{}](HW:/led/solid:{...})` (LLM link-wrapped
		// only the second marker) the label is `HW:/led/effect/stop:{}`.
		// Reconstruct BOTH markers in emitted order instead of demoting the
		// first one to spoken text.
		if len(label) >= 3 && strings.EqualFold(label[:3], "HW:") {
			return "[HW:" + label[3:] + "]" + marker
		}
		if strings.TrimSpace(label) == "" {
			return marker
		}
		return marker + " " + label
	})
}

type hwCall struct {
	path string
	body string
}

// extractHWCalls parses all [HW:/path:{"json"}] markers from text,
// returns the list of calls and the text with all markers stripped.
func extractHWCalls(text string) ([]hwCall, string) {
	text = normalizeHWMarkers(text)
	matches := hwMarkerRe.FindAllStringSubmatch(text, -1)
	calls := make([]hwCall, 0, len(matches))
	for _, m := range matches {
		body := m[2]
		if body == "" { // bodyless marker like [HW:/led/off] → default to empty JSON
			body = "{}"
		}
		// Normalize colon-separated path segments to slash (e.g. /audio:play →
		// /audio/play). Some LLM backends emit colons instead of slashes as path
		// separators; normalizing here makes the parser tolerant of any backend.
		path := "/" + strings.ReplaceAll(strings.TrimPrefix(m[1], "/"), ":", "/")
		calls = append(calls, hwCall{path: path, body: body})
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
	text = normalizeHWMarkers(text)
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
		rawPath := text[m[2]:m[3]]
		path := "/" + strings.ReplaceAll(strings.TrimPrefix(rawPath, "/"), ":", "/")
		calls = append(calls, hwCall{path: path, body: body})
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
	// A cancelled turn keeps its remote delivery but loses the body. The click
	// means "stop talking to ME", so it must not swallow a reply addressed to a
	// Telegram user — which is why this gate sits AFTER the /dm and /broadcast
	// markers above and only covers real hardware. Without it the device goes
	// quiet but keeps moving: servos finishing a gesture and LEDs changing colour
	// after the user asked it to stop reads as "it ignored me".
	if h.isSpeechCancelled(flowRunID) {
		slog.Info("HW marker dropped -- turn cancelled by physical gesture",
			"component", "agent", "run_id", flowRunID, "path", c.path)
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
	// os-server-bound HW markers (log writes that live in os-server, not HAL):
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
			// run_id included so a fired marker can be attributed to its turn:
			// without it, "speech muted but the body still moved" is invisible in
			// the log — the drop line names a run and the fire line named none.
			slog.Info("HW marker fired", "component", "openclaw", "path", c.path, "run_id", flowRunID)
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
		// Lock/unlock ambient breathing, mirroring the session-tool path
		// (handler_event_session_tool.go). Without this, an agent reply
		// marker like [HW:/led/effect:{"effect":"rainbow"}] never sets
		// ambient's ledLocked, and ~60s after the turn ambient's breathing
		// resumes and tramples the agent-set color/effect.
		switch {
		case strings.Contains(c.path, "/led/off"), strings.Contains(c.path, "/scene/off"):
			h.monitorBus.Push(domain.MonitorEvent{Type: "led_off", Summary: "agent hw: " + c.path})
		case strings.Contains(c.path, "/led/effect/stop"), strings.Contains(c.path, "/led/restore"):
			// stop/restore release the strip — neither a deliberate look
			// (no lock) nor a turn-off (no unlock).
		default:
			h.monitorBus.Push(domain.MonitorEvent{Type: "led_set", Summary: "agent hw: " + c.path})
		}
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

// ADDED 2026-07-23: fireEchoedHWMarkers rescues HW markers the agent wrapped
// inside a shell tool call instead of emitting them as reply text — e.g.
// `echo '[HW:/audio/play:{"query":"..."}]'` run through the exec/bash tool. A
// shell `echo` only prints the marker to stdout inside the agent sandbox; it
// never reaches the reply-text marker interceptor (extractHWCalls on the
// assistant message), so the command silently no-ops. The flow node still lit
// up because the tool args contain the marker string ("node sáng mà câm loa"),
// while HAL received nothing and no music played.
//
// Detect any [HW:...] markers echoed in the tool args and fire them for real.
// extractHWCalls matches ONLY the [HW:/path:{json}] grammar, so a legitimate
// `curl .../audio/play` (no marker) is NOT caught here and still plays via its
// own request — no double fire. Returns true when it fired, so the caller can
// skip the cosmetic-only flow node emit (fireHWCall already emits the
// authoritative hw_* events with the resolved path).
func (h *AgentHandler) fireEchoedHWMarkers(toolName, toolArgs, flowRunID string) bool {
	calls, _ := extractHWCalls(toolArgs)
	if len(calls) == 0 {
		return false
	}
	paths := make([]string, 0, len(calls))
	for _, c := range calls {
		paths = append(paths, c.path)
	}
	slog.Warn("agent echoed HW marker(s) inside a tool call instead of emitting them as reply text — firing to HAL so the action is not silently dropped",
		"component", "agent-hw", "tool", toolName, "paths", paths, "run_id", flowRunID)
	h.fireHWCalls(calls, flowRunID)
	return true
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

// ADDED 2026-05-26, REWORKED 2026-07-06: fireHWCallsSync fires the markers
// sequentially in ONE goroutine (5s per call) and only WAITS for completion
// up to 100ms — the stream-time budget. Used when subsequent TTS depends on
// HAL state mutations (e.g. /scene/off must unmute speaker before
// /voice/speak): fast markers complete inside the wait, slow ones keep
// running in order behind the TTS.
//
// Two hard-won invariants:
//   - ORDER. The old per-call 100ms client timeout re-fired a slow call
//     async and let the sync loop keep going, so later markers OVERTOOK it:
//     [/servo/resume][/servo/nudge][/servo/hold][/emotion] had nudge (~2s in
//     HAL) land AFTER hold and emotion — the emotion's servo animation
//     slipped in before the hold flag was set, played over the late nudge
//     and parked the arm at ITS final pose ("turn right 45° and crouch"
//     visibly turned, snapped back, never crouched). Markers encode intent
//     in their emitted order; never let one leapfrog.
//   - NO ABORT/RETRY. Cutting a request at 100ms and re-firing it risks
//     double execution if HAL processed the aborted one anyway — fatal for
//     relative commands (/servo/nudge +45 twice = +90). Letting the original
//     request run to completion needs no retry at all.
func (h *AgentHandler) fireHWCallsSync(calls []hwCall, flowRunID string) {
	if len(calls) == 0 {
		return
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		client := &http.Client{Timeout: 5 * time.Second}
		for _, c := range calls {
			h.fireHWCall(c, flowRunID, client)
		}
	}()
	select {
	case <-done:
	case <-time.After(100 * time.Millisecond):
		// Budget spent — TTS proceeds; the goroutine finishes the tail in order.
	}
}
