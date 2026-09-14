package http

import (
	"encoding/json"
	"testing"

	"go.autonomous.ai/os/system/domain"
)

func TestExternalHistoryObserverOnlyReportsTerminalCorrelatedEvents(t *testing.T) {
	for _, tc := range []struct {
		event, payload string
		called, failed bool
	}{
		{"agent", `{"runId":"device-chat-context-a","stream":"lifecycle","data":{"phase":"end"}}`, true, false},
		{"agent", `{"runId":"device-chat-context-a","stream":"lifecycle","data":{"phase":"end","aborted":true}}`, true, true},
		{"agent", `{"runId":"device-chat-context-a","stream":"lifecycle","data":{"phase":"error"}}`, true, true},
		{"chat", `{"runId":"device-chat-context-a","state":"final"}`, false, false},
		{"chat", `{"run_id":"device-chat-context-a","state":"error"}`, true, true},
		{"chat", `{"runId":"device-chat-context-a","state":"delta"}`, false, false},
		{"agent", `{"runId":"device-chat-context-a","stream":"lifecycle","data":{"phase":"start"}}`, false, false},
		{"session.message", `{"sessionKey":"main","message":{"role":"assistant","content":"NO_REPLY"}}`, false, false},
		{"chat", `{"state":"final"}`, false, false},
	} {
		t.Run(tc.event+tc.payload, func(t *testing.T) {
			h := &AgentHandler{}
			called := false
			h.SetExternalHistoryObserver(func(id string, failed bool) {
				called = true
				if id != "device-chat-context-a" || failed != tc.failed {
					t.Fatalf("%s %v", id, failed)
				}
			})
			h.observeExternalHistory(domain.WSEvent{Event: tc.event, Payload: json.RawMessage(tc.payload)})
			if called != tc.called {
				t.Fatal("unexpected callback", called)
			}
		})
	}
}
