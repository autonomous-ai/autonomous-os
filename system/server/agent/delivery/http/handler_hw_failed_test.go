package http

import (
	"errors"
	"net/http"
	"testing"

	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
)

// failingTransport stands in for the 5 s client timeout without waiting 5 s:
// every request fails at the transport, which is the same error path a timeout
// takes (`client.Post` returns err, no response).
type failingTransport struct{}

func (failingTransport) RoundTrip(*http.Request) (*http.Response, error) {
	return nil, errors.New("context deadline exceeded (Client.Timeout exceeded while awaiting headers)")
}

// A hardware POST that failed at the transport used to `return false` BEFORE
// reaching any flow.Log, so a 40 s body movement left no trace in the monitor
// at all — device-chat-44 emitted a marker, HAL swept the room, and the flow
// log showed nothing (#342 defects H, K). A failure must be an event.
func TestATransportFailureIsLoggedAsAFlowEvent(t *testing.T) {
	// No flow.Init: the ring buffer fills regardless, and Init would start
	// the JSONL writer, which drops local/flow_events_*.jsonl into whatever
	// directory the test runs from.
	h := &AgentHandler{
		monitorBus: monitor.ProvideBus(),
		config:     &config.Config{DeviceType: "lamp"},
	}
	before := len(flow.Recent(1000))

	ok := h.fireHWCall(hwCall{path: "/servo/search", body: `{"target":"keyboard"}`},
		"device-chat-44", &http.Client{Transport: failingTransport{}})

	if ok {
		t.Fatal("a failed POST reported success")
	}
	var found *flow.Event
	for _, ev := range flow.Recent(1000)[before:] {
		if ev.Node == "hw_failed" {
			found = &ev
			break
		}
	}
	if found == nil {
		t.Fatal("no hw_failed event — the failure is invisible to the monitor")
	}
	if found.TraceID != "device-chat-44" {
		t.Errorf("hw_failed carries run %q, want device-chat-44", found.TraceID)
	}
	if found.Data["path"] != "/servo/search" {
		t.Errorf("hw_failed path = %v, want /servo/search", found.Data["path"])
	}
	if e, _ := found.Data["error"].(string); e == "" {
		t.Error("hw_failed carries no error text — a reader cannot tell a timeout from a refused connection")
	}
}
