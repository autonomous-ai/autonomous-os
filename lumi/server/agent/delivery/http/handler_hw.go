package http

import (
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"regexp"
	"strings"

	"go-lamp.autonomous.ai/domain"
	"go-lamp.autonomous.ai/lib/flow"
	"go-lamp.autonomous.ai/lib/i18n"
	"go-lamp.autonomous.ai/lib/lelamp"
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
var hwMarkerRe = regexp.MustCompile(`\[HW:(/[^:]+):(\{[^}]*\})\]`)

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
		calls = append(calls, hwCall{path: m[1], body: m[2]})
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
		slog.Info("HW markers extracted", "component", "openclaw", "count", len(calls), "paths", paths)
	}
	return calls, strings.TrimSpace(hwMarkerRe.ReplaceAllString(text, ""))
}

// fireHWCalls fires hardware calls to LeLamp sequentially in a goroutine,
// with full flow tracking, lastEmotion update, and monitorBus events.
// Sequential order matters (e.g. emotion sequences must fire in order).
func (h *AgentHandler) fireHWCalls(calls []hwCall, flowRunID string) {
	if len(calls) == 0 {
		return
	}
	go func() {
		for _, c := range calls {
			// /broadcast, /speak, /dm are internal control markers — not LeLamp endpoints.
			if c.path == "/broadcast" || c.path == "/speak" || c.path == "/dm" {
				continue
			}
			// Lumi-bound HW markers (log writes that live on Lumi, not LeLamp):
			//   /wellbeing/log         → POST :5000/api/wellbeing/log
			//   /mood/log              → POST :5000/api/mood/log (signal + decision share endpoint, kind in body)
			//   /music-suggestion/log  → POST :5000/api/music-suggestion/log
			//   /posture/log           → POST :5000/api/posture/log (nudge/praise/recap rows)
			// Lets the agent fire side-effect POSTs without consuming a tool
			// turn — fireHWCalls runs async in this goroutine and the reply
			// text is already on its way to TTS.
			postURL := lelamp.BaseURL + c.path
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
			resp, err := http.Post(postURL, "application/json", strings.NewReader(c.body))
			if err != nil {
				slog.Warn("HW marker call failed", "component", "openclaw", "path", c.path, "error", err)
				continue
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
					if err := lelamp.Speak(trackFailMessage(target)); err != nil {
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
				// music.play logged via flow.Log above
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
		}
	}()
}
