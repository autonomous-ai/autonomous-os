package http

import (
	"testing"

	"go.autonomous.ai/os/system/monitor"
)

func TestHarnessWebResponseIsDisplayedWithoutTTS(t *testing.T) {
	bus := monitor.ProvideBus()
	events, unsubscribe := bus.Subscribe()
	defer unsubscribe()
	h := &AgentHandler{monitorBus: bus}
	h.MarkHarnessResponseRun("device-chat-42", true)
	if !h.DeliverHarnessResponse("device-chat-42", "Final recap from Harness") {
		t.Fatal("Harness response was not delivered")
	}
	event := <-events
	if event.Type != "chat_response" || event.State != "final" || event.RunID != "device-chat-42" || event.Summary != "Final recap from Harness" {
		t.Fatalf("event = %#v", event)
	}
	if h.DeliverHarnessResponse("device-chat-42", "duplicate") {
		t.Fatal("delivered the same Harness response twice")
	}
}

func TestHarnessProgressKeepsTheResponsePending(t *testing.T) {
	bus := monitor.ProvideBus()
	events, unsubscribe := bus.Subscribe()
	defer unsubscribe()
	h := &AgentHandler{monitorBus: bus}
	h.MarkHarnessResponseRun("device-chat-42", true)
	if !h.DeliverHarnessProgress("device-chat-42", "Harness agent is working.") {
		t.Fatal("Harness progress was not delivered")
	}
	event := <-events
	if event.Type != "assistant_delta" || event.RunID != "device-chat-42" || event.Summary != "Harness agent is working." {
		t.Fatalf("event = %#v", event)
	}
}
