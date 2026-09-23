package http

import (
	"testing"

	"go.autonomous.ai/os/system/monitor"
)

func TestHarnessPreparationDoesNotOwnMainResponse(t *testing.T) {
	bus := monitor.ProvideBus()
	events, unsubscribe := bus.Subscribe()
	defer unsubscribe()
	h := &AgentHandler{monitorBus: bus, activeRunIDBySession: map[string]string{"session": "device-chat-42"}}
	if !h.DeliverHarnessPreparationProgress("device-chat-42", "Harness needs user action.") {
		t.Fatal("active progress dropped")
	}
	event := <-events
	if event.Type != "assistant_delta" || event.RunID != "device-chat-42" || event.Summary != "Harness needs user action." {
		t.Fatalf("event = %+v", event)
	}
	if h.suppressHarnessAgentReply("device-chat-42") {
		t.Fatal("preparation suppressed main agent guidance")
	}
	if h.DeliverHarnessPreparationProgress("other-run", "wrong") {
		t.Fatal("progress crossed runs")
	}
	delete(h.activeRunIDBySession, "session")
	if h.DeliverHarnessPreparationProgress("device-chat-42", "late") {
		t.Fatal("progress replaced completed main response")
	}
}

func TestHarnessPreparationCannotReplaceRemoteTaskResponse(t *testing.T) {
	h := &AgentHandler{activeRunIDBySession: map[string]string{"session": "device-chat-42"}}
	h.MarkHarnessResponseRun("device-chat-42", true, false)
	if h.DeliverHarnessPreparationProgress("device-chat-42", "late ready") {
		t.Fatal("preparation replaced task progress")
	}
	if !h.DeliverHarnessResponse("device-chat-42", "Final task result") {
		t.Fatal("final dropped")
	}
	if h.DeliverHarnessPreparationProgress("device-chat-42", "late ready") {
		t.Fatal("preparation replaced final")
	}
}

func TestHarnessPreparationResolvesNativeRunID(t *testing.T) {
	h := &AgentHandler{activeRunIDBySession: map[string]string{"session": "native-id"}, runIDMap: map[string]string{"native-id": "device-chat-42"}}
	if !h.DeliverHarnessPreparationProgress("device-chat-42", "Preparing") {
		t.Fatal("resolved active run dropped")
	}
}
