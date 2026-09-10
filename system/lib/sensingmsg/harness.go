package sensingmsg

import "strings"

// AppendHarnessReplyRoute preserves the device response address after queue replay.
// The model must copy this address, never invent a descriptive device-chat ID.
func AppendHarnessReplyRoute(msg, eventType, runID string) string {
	channel := ""
	switch eventType {
	case "web_chat", "mqtt_chat":
		channel = "web"
	case "voice_followup":
		channel = "voice"
	}
	if channel == "" || runID == "" {
		return msg
	}
	route := "[harness-reply run_id=" + runID + " channel=" + channel + "]"
	if strings.Contains(msg, route) {
		return msg
	}
	return msg + "\n" + route
}
