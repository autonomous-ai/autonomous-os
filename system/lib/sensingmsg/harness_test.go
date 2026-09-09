package sensingmsg

import (
	"strings"
	"testing"
)

func TestReplayHarnessResponseAddress(t *testing.T) {
	for _, typ := range []string{"web_chat", "mqtt_chat", "voice_followup"} {
		got := AppendHarnessReplyRoute("[user] ask browser to work", typ, "device-chat-42-1234")
		channel := "web"
		if typ == "voice_followup" {
			channel = "voice"
		}
		if !strings.Contains(got, "[harness-reply run_id=device-chat-42-1234 channel="+channel+"]") {
			t.Fatalf("%s lost response address: %s", typ, got)
		}
	}
	if got := AppendHarnessReplyRoute("sound", "sound", "run"); got != "sound" {
		t.Fatal(got)
	}
}
