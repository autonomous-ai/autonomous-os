package sse

import (
	"encoding/json"
	"log/slog"
	"regexp"
	"strings"
)

var emotionRe = regexp.MustCompile(`(?:\\"|")emotion(?:\\"|")\s*:\s*(?:\\"|")([a-zA-Z_]+)(?:\\"|")`)

// parseEmotion extracts the emotion name from a tool call args string.
// Handles both plain JSON ("emotion": "sad") and escaped JSON (\"emotion\": \"sad\").
func parseEmotion(toolArgs string) string {
	if m := emotionRe.FindStringSubmatch(toolArgs); len(m) == 2 {
		return m[1]
	}
	return ""
}

// extractTTSText parses the text argument from an OpenClaw built-in tts tool call.
// Args can be JSON like {"text":"hello"} or a plain string.
func extractTTSText(toolArgs string) string {
	var obj struct {
		Text string `json:"text"`
	}
	if json.Unmarshal([]byte(toolArgs), &obj) == nil && obj.Text != "" {
		return obj.Text
	}
	return strings.TrimSpace(toolArgs)
}

// isAgentNoReply returns true if text is an OpenClaw framework "silent" sentinel
// (e.g. "NO_REPLY", "NO_RE") or a bare "NO" the LLM sometimes emits instead.
// These should never be spoken aloud or shown to the user.
func isAgentNoReply(text string) bool {
	t := strings.TrimSpace(strings.ToUpper(text))
	if t == "NO" {
		slog.Warn("agent emitted bare NO instead of NO_REPLY — suppressing TTS", "component", "agent", "raw", text)
		return true
	}
	if strings.HasPrefix(t, "NO_") {
		slog.Warn("agent no-reply sentinel — suppressing TTS", "component", "agent", "raw", text)
		return true
	}
	return false
}

// sanitizeAgentText strips internal sentinels the LLM sometimes appends to real replies.
// e.g. "Hello! NO_REPLY" → "Hello!", "...done! HEARTBEAT_OK" → "...done!"
func sanitizeAgentText(text string) string {
	for _, sentinel := range []string{"NO_REPLY", "HEARTBEAT_OK"} {
		if idx := strings.LastIndex(strings.ToUpper(text), sentinel); idx >= 0 {
			cleaned := strings.TrimRight(text[:idx], " \t\n!.,—–-")
			if cleaned != "" {
				slog.Warn("stripped trailing sentinel from agent text", "component", "agent", "sentinel", sentinel, "before", text[:min(len(text), 100)], "after", cleaned)
				text = cleaned
			}
		}
	}
	return text
}

// sayTagRe captures the content between the first <say>...</say> pair.
// The (?s) flag lets `.` match newlines so multi-line content is supported.
var sayTagRe = regexp.MustCompile(`(?s)<say>(.*?)</say>`)

// extractSayTag pulls the spoken sentence out of a <say>...</say> wrapper.
// Skills (currently wellbeing) instruct the model to wrap the one caring sentence
// in <say> tags so its free-form reasoning in the text block doesn't leak to TTS.
// Passthrough when no tag is present so skills that don't opt in stay unchanged.
// Empty tag (`<say></say>`) collapses to NO_REPLY.
func extractSayTag(text string) string {
	m := sayTagRe.FindStringSubmatch(text)
	if m == nil {
		return text
	}
	inner := strings.TrimSpace(m[1])
	if inner == "" {
		slog.Info("empty <say> tag — treating as NO_REPLY", "component", "agent")
		return "NO_REPLY"
	}
	slog.Info("extracted <say> tag", "component", "agent", "before_len", len(text), "after", inner[:min(len(inner), 100)])
	return inner
}

// isLumiOutboundChatRunID is true when runID matches Lumi's chat.send idempotency key
// (lumi-chat-* current; lumi-sensing-* legacy). Used so traceless lifecycle_start is not
// mis-tagged as Telegram-only when the turn was initiated from Lumi.
func isLumiOutboundChatRunID(runID string) bool {
	if runID == "" {
		return false
	}
	return strings.HasPrefix(runID, "lumi-chat-") || strings.HasPrefix(runID, "lumi-sensing-")
}

// labelForLumiInternal returns the UI label that best describes a Lumi-
// internal message (sensing/voice/wellbeing/system events Lumi posts via
// chat.send). Used by the Flow Monitor channel-turn handler to avoid
// mis-labelling steer-merged self-fire turns as `[telegram]` when they
// are actually sensing or voice events Lumi originated.
//
// Returns "" when the text doesn't match any known internal prefix —
// caller should fall back to the configured-channel label in that case.
func labelForLumiInternal(text string) string {
	switch {
	case strings.HasPrefix(text, "[user] [ambient]"),
		strings.HasPrefix(text, "[ambient]"),
		strings.HasPrefix(text, "[user]"):
		return "[voice]"
	case strings.HasPrefix(text, "[emotion]"):
		return "[emotion]"
	case strings.HasPrefix(text, "[speech_emotion]"):
		return "[speech_emotion]"
	case strings.HasPrefix(text, "[activity]"):
		return "[activity]"
	case strings.HasPrefix(text, "[wellbeing]"):
		return "[wellbeing]"
	case strings.HasPrefix(text, "[music-proactive]"):
		return "[music]"
	case strings.HasPrefix(text, "[system]"):
		return "[system]"
	case strings.HasPrefix(text, "[sensing:"):
		return "[sensing]"
	}
	return ""
}

// isChannelOriginatedRun returns true only when any of the given runIDs was
// synthesised by Lumi from a real external channel user message — currently
// "tg-<msgID>" created in the session.message handler when OpenClaw forwards
// a Telegram user turn (see handler_events.go ~line 1157).
//
// This is the positive-evidence signal for "real channel user", replacing the
// older "anything NOT lumi-chat-*" default which mis-classified UUID runs
// from OpenClaw steer-mode self-fire / cron / heartbeat as Telegram and
// suppressed their TTS even when no real user was on the other end.
//
// The channelRuns map override (chat.history fallback) remains the safety net
// for any future case where a real channel user shows up under a non-tg-
// runID before this helper recognises it.
func isChannelOriginatedRun(runIDs ...string) bool {
	for _, r := range runIDs {
		if strings.HasPrefix(r, "tg-") {
			return true
		}
	}
	return false
}

// extractMessageContentText collects text from a session.message `content`
// field. OpenClaw emits content as either a plain string or an array of typed
// blocks ({"type":"text","text":"..."} / {"type":"toolCall",...}). Only
// `type == "text"` blocks contribute to the joined output; tool blocks
// carry no spoken text.
func extractMessageContentText(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var s string
	if err := json.Unmarshal(raw, &s); err == nil {
		return s
	}
	var blocks []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	if err := json.Unmarshal(raw, &blocks); err != nil {
		return ""
	}
	parts := make([]string, 0, len(blocks))
	for _, b := range blocks {
		if b.Type == "text" && strings.TrimSpace(b.Text) != "" {
			parts = append(parts, b.Text)
		}
	}
	return strings.Join(parts, "")
}

// lumiInternalPrefixes are message-text prefixes Lumi puts on chat.sends it
// issues itself (sensing events, ambient voice, activity, emotion cues,
// wellbeing nudges, wake greetings). Used as a robust guard alongside
// IsRecentOutboundChat — that exact-match buffer can miss when the 30s
// window expires or 32-entry cap overflows under load. Any text starting
// with one of these prefixes is definitely Lumi-internal, never a real
// Telegram user message, and must NOT mark the run as a channel turn.
var lumiInternalPrefixes = []string{
	"[sensing:",
	"[ambient]",
	"[activity]",
	"[emotion]",
	"[speech_emotion]",
	"[wellbeing]",
	"[music-proactive]",
	"[system]",
	"You just woke up",
	"Bạn vừa thức dậy",
	"你刚刚醒来",
	"你剛剛醒來",
}

// isLumiInternalMessage returns true when the message text was issued by
// Lumi via chat.send (matches a known prefix). The check is independent of
// the recent-outbound TTL buffer so it stays correct under burst load.
func isLumiInternalMessage(text string) bool {
	if text == "" {
		return false
	}
	for _, p := range lumiInternalPrefixes {
		if strings.HasPrefix(text, p) {
			return true
		}
	}
	return false
}

// telegramChatIDRe extracts the chat_id from OpenClaw queue-mode metadata
// injected at the top of a Telegram-originated user message, e.g.
// `"chat_id": "telegram:158406741"`.
var telegramChatIDRe = regexp.MustCompile(`"chat_id"\s*:\s*"telegram:(\d+)"`)

// extractTelegramChatID returns the numeric Telegram chat_id from a session
// message body when OpenClaw injected the conversation metadata block; "" if
// the marker is absent (non-Telegram message or older OpenClaw format).
func extractTelegramChatID(text string) string {
	if m := telegramChatIDRe.FindStringSubmatch(text); len(m) == 2 {
		return m[1]
	}
	return ""
}

// senderLabelTelegramIDRe captures the numeric id from senderLabel formats
// emitted by OpenClaw, e.g. "Leo (@squall_leo_hart) id:158406741" or
// "Leo (158406741)". Used as fallback when the message content lacks the
// `chat_id: telegram:<id>` metadata block (sometimes injected, sometimes not).
var senderLabelTelegramIDRe = regexp.MustCompile(`(?:id:|\()(\d{6,})\)?`)

// extractTelegramIDFromSenderLabel returns the numeric Telegram user ID found
// in a senderLabel string. Returns "" if no id-like substring matches.
func extractTelegramIDFromSenderLabel(label string) string {
	if label == "" {
		return ""
	}
	if m := senderLabelTelegramIDRe.FindStringSubmatch(label); len(m) == 2 {
		return m[1]
	}
	return ""
}

// shortError extracts a short, readable message from a potentially large error string.
// Strips HTML bodies (e.g. Cloudflare 403 pages) down to the status line.
func shortError(errMsg string) string {
	// Extract leading status code + domain if it looks like "403 <!DOCTYPE..."
	if idx := strings.Index(errMsg, "<!"); idx > 0 {
		prefix := strings.TrimSpace(errMsg[:idx])
		// Try to find domain from <h2> "unable to access X"
		if i := strings.Index(errMsg, "unable_to_access"); i > 0 {
			if j := strings.Index(errMsg[i:], ">"); j > 0 {
				if k := strings.Index(errMsg[i+j:], "<"); k > 0 {
					domain := strings.TrimSpace(errMsg[i+j+1 : i+j+k])
					if domain != "" {
						return prefix + " blocked by Cloudflare (" + domain + ")"
					}
				}
			}
		}
		return prefix + " (HTML error page)"
	}
	if len(errMsg) > 120 {
		return errMsg[:120] + "..."
	}
	return errMsg
}
