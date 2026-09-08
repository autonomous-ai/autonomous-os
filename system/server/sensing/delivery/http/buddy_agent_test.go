package http

import (
	"go.autonomous.ai/os/system/lib/speakergate"
	"testing"
)

func TestBuddyAgentCompletionQueuesAndWaitsForSpeaker(t *testing.T) {
	for _, kind := range []string{"buddy.agent.session-a", "buddy.agent.session-b"} {
		if !shouldQueueEvent(kind, "completed", false) {
			t.Fatal("completion dropped while busy", kind)
		}
		if !speakergate.WaitsForSpeaker(kind) {
			t.Fatal("completion interrupts current reply", kind)
		}
	}
}
