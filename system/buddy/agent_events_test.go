package buddy

import (
	"encoding/json"
	"github.com/gorilla/websocket"
	"strings"
	"testing"
)

func TestAgentEventsValidateBindDeduplicate(t *testing.T) {
	r := NewRegistry()
	current := &websocket.Conn{}
	r.conn = current
	s := &Service{registry: r}
	event := AgentEvent{Type: "agent_event", ProjectID: "project-1", SessionID: "session-1", Seq: 2, Status: "completed", Title: "Task", Summary: "Done"}
	encode := func(e AgentEvent) []byte {
		b, err := json.Marshal(e)
		if err != nil {
			t.Fatal(err)
		}
		return b
	}
	if _, ok := s.acceptAgentEvent(&websocket.Conn{}, "buddy", encode(event)); ok {
		t.Fatal("accepted stale socket")
	}
	if _, ok := s.acceptAgentEvent(current, "buddy", encode(event)); !ok {
		t.Fatal("rejected valid event")
	}
	if _, ok := s.acceptAgentEvent(current, "buddy", encode(event)); ok {
		t.Fatal("accepted replay")
	}
	older := event
	older.Seq = 1
	if _, ok := s.acceptAgentEvent(current, "buddy", encode(older)); ok {
		t.Fatal("accepted older event")
	}
	s.RetryAgentEvent("buddy", older)
	if _, ok := s.acceptAgentEvent(current, "buddy", encode(event)); ok {
		t.Fatal("old failure cleared newer cursor")
	}
	s.RetryAgentEvent("buddy", event)
	if _, ok := s.acceptAgentEvent(current, "buddy", encode(event)); !ok {
		t.Fatal("failed delivery cannot retry")
	}
	for _, change := range []func(*AgentEvent){
		func(e *AgentEvent) { e.Seq = 0 }, func(e *AgentEvent) { e.SessionID = "../../oops" },
		func(e *AgentEvent) { e.Status = "running" }, func(e *AgentEvent) { e.Title = strings.Repeat("a", 513) },
		func(e *AgentEvent) { e.Summary = strings.Repeat("a", 8193) },
	} {
		bad := event
		bad.Seq = 3
		change(&bad)
		if _, ok := s.acceptAgentEvent(current, "buddy", encode(bad)); ok {
			t.Fatalf("accepted invalid event: %#v", bad)
		}
	}
	if _, ok := s.acceptAgentEvent(current, "buddy", []byte(strings.Repeat(" ", 16385))); ok {
		t.Fatal("accepted oversized frame")
	}
	if _, ok := s.acceptAgentEvent(nil, "buddy", encode(event)); ok {
		t.Fatal("accepted missing socket")
	}
}
