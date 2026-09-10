package http

import (
	"encoding/json"
	"testing"

	"go.autonomous.ai/os/system/domain"

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

func TestHarnessToolIsShownWhileResponseIsPending(t *testing.T) {
	bus := monitor.ProvideBus()
	events, unsubscribe := bus.Subscribe()
	defer unsubscribe()
	h := &AgentHandler{monitorBus: bus}
	h.MarkHarnessResponseRun("device-chat-42", true)
	if !h.DeliverHarnessTool("device-chat-42", "web_search", "restaurants in Hanoi") {
		t.Fatal("Harness tool was not delivered")
	}
	event := <-events
	if event.Type != "assistant_delta" || event.RunID != "device-chat-42" || event.Summary != "Harness is web_search." {
		t.Fatalf("event = %#v", event)
	}
}

func TestHarnessHandoffDoesNotCloseChatBeforeResult(t *testing.T) {
	bus := monitor.ProvideBus()
	events, unsubscribe := bus.Subscribe()
	defer unsubscribe()
	h := &AgentHandler{monitorBus: bus}
	h.MarkHarnessResponseRun("test-run", true)
	payload, _ := json.Marshal(map[string]string{"runId": "test-run", "role": "assistant", "state": "final", "message": "Task sent. NO_REPLY"})
	if err := h.handleChatEvent(domain.WSEvent{Payload: payload}); err != nil {
		t.Fatal(err)
	}
	select {
	case event := <-events:
		t.Fatalf("handoff closed chat: %#v", event)
	default:
	}
	if !h.DeliverHarnessResponse("test-run", "Full Harness result") {
		t.Fatal("missing result")
	}
	event := <-events
	if event.Summary != "Full Harness result" || event.RunID != "test-run" {
		t.Fatalf("wrong result: %#v", event)
	}
}

func TestHarnessReplyCannotConsumeAnotherChat(t *testing.T) {
	h := &AgentHandler{monitorBus: monitor.ProvideBus()}
	h.MarkHarnessResponseRun("web-a", true)
	h.MarkHarnessResponseRun("web-b", true)
	if h.suppressHarnessAgentReply("unrelated") {
		t.Fatal("unrelated chat suppressed by pending Harness task")
	}
	if h.DeliverHarnessResponse("unknown", "wrong result") {
		t.Fatal("unknown result consumed another chat")
	}
	if h.DeliverHarnessResponse("web-a", "") {
		t.Fatal("empty result accepted")
	}
	if !h.DeliverHarnessResponse("web-a", "answer A") {
		t.Fatal("empty result consumed route A")
	}
	if !h.DeliverHarnessResponse("web-b", "answer B") {
		t.Fatal("route B was consumed by another result")
	}
}

func TestHarnessFinalSurvivesRuntimeLifecycleOrdering(t *testing.T) {
	for _, resultFirst := range []bool{false, true} {
		t.Run(map[bool]string{false: "runtime-first", true: "result-first"}[resultFirst], func(t *testing.T) {
			bus := monitor.ProvideBus()
			events, unsubscribe := bus.Subscribe()
			defer unsubscribe()
			h := &AgentHandler{monitorBus: bus}
			h.MarkHarnessResponseRun("test-run", true)
			if resultFirst {
				h.DeliverHarnessResponse("test-run", "Harness final")
			}
			if !h.suppressHarnessAgentReply("test-run") {
				t.Fatal("runtime handoff not suppressed")
			}
			h.clearHarnessResponseRun("test-run")
			if !resultFirst && !h.DeliverHarnessResponse("test-run", "Harness final") {
				t.Fatal("late Harness final lost")
			}
			if h.DeliverHarnessProgress("test-run", "late progress") {
				t.Fatal("progress emitted after final")
			}
			// OpenClaw can emit its generic chat final after lifecycle:end.
			payload, _ := json.Marshal(map[string]string{"runId": "test-run", "role": "assistant", "state": "final", "message": "NO_REPLY"})
			h.handleChatEvent(domain.WSEvent{Payload: payload})
			event := <-events
			if event.Summary != "Harness final" {
				t.Fatalf("wrong final: %+v", event)
			}
			select {
			case extra := <-events:
				t.Fatalf("extra response after Harness final: %+v", extra)
			default:
			}
		})
	}
}
