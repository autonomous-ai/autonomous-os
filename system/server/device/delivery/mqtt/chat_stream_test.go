package mqtthandler

import (
	"encoding/json"
	"sync"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

// newTestStream builds a stream whose transport captures payloads instead of
// reaching a broker.
func newTestStream() (*ChatStream, func() []domain.MQTTChatEventData) {
	var mu sync.Mutex
	var sent []domain.MQTTChatEventData

	s := &ChatStream{
		cfg:  &config.Config{DeviceID: "dev1", FDChannel: "fd/dev1"},
		runs: map[string]*trackedRun{},
	}
	s.publish = func(body []byte) error {
		var resp struct {
			Kind string                   `json:"kind"`
			Data domain.MQTTChatEventData `json:"data"`
		}
		if err := json.Unmarshal(body, &resp); err != nil {
			return err
		}
		mu.Lock()
		defer mu.Unlock()
		sent = append(sent, resp.Data)
		return nil
	}

	return s, func() []domain.MQTTChatEventData {
		mu.Lock()
		defer mu.Unlock()
		out := make([]domain.MQTTChatEventData, len(sent))
		copy(out, sent)
		return out
	}
}

// The bus carries every turn on the device, including voice ones. Only runs the
// backend started may be mirrored — otherwise a spoken conversation in the room
// would stream to whoever last opened the app.
func TestChatStreamIgnoresUntrackedRuns(t *testing.T) {
	s, sent := newTestStream()

	s.handle(domain.MonitorEvent{Type: "chat_response", RunID: "not-mine", Summary: "hi"})
	s.handle(domain.MonitorEvent{Type: "assistant_delta", RunID: "", Summary: "orphan"})
	s.flushAll()

	if got := sent(); len(got) != 0 {
		t.Fatalf("published %d events for untracked runs: %+v", len(got), got)
	}
}

// Deltas are accumulated, not forwarded 1:1 — at QoS 1 each publish is a
// round-trip, and the bus emits one delta per model chunk.
func TestChatStreamCoalescesDeltas(t *testing.T) {
	s, sent := newTestStream()
	s.Track("run1", "sess1")

	for _, chunk := range []string{"Hello", " there", ", Leo"} {
		s.handle(domain.MonitorEvent{Type: "assistant_delta", RunID: "run1", Summary: chunk})
	}
	if got := sent(); len(got) != 0 {
		t.Fatalf("deltas published before a flush: %+v", got)
	}

	s.flushAll()

	got := sent()
	if len(got) != 1 {
		t.Fatalf("want 1 coalesced event, got %d: %+v", len(got), got)
	}
	if got[0].Event.Summary != "Hello there, Leo" {
		t.Errorf("text = %q, want the three chunks joined", got[0].Event.Summary)
	}
	if got[0].SessionID != "sess1" {
		t.Errorf("session = %q, want it echoed on every event", got[0].SessionID)
	}
	if got[0].RunID != "run1" {
		t.Errorf("run id = %q", got[0].RunID)
	}
	// A coalesced event is not any one of the chunks it was built from, so it
	// must not reuse their id.
	if got[0].Event.ID != "" {
		t.Errorf("coalesced event kept id %q", got[0].Event.ID)
	}
}

// A tool chip must not overtake the sentence that preceded it: pending delta
// text is flushed before any other event goes out.
func TestChatStreamFlushesPendingBeforeOtherEvents(t *testing.T) {
	s, sent := newTestStream()
	s.Track("run1", "sess1")

	s.handle(domain.MonitorEvent{Type: "assistant_delta", RunID: "run1", Summary: "Taking a photo"})
	s.handle(domain.MonitorEvent{Type: "tool_call", RunID: "run1", Summary: "Tool Bash"})

	got := sent()
	if len(got) != 2 {
		t.Fatalf("want 2 events, got %d: %+v", len(got), got)
	}
	if got[0].Event.Type != "assistant_delta" || got[0].Event.Summary != "Taking a photo" {
		t.Errorf("first event = %+v, want the buffered text", got[0].Event)
	}
	if got[1].Event.Type != "tool_call" {
		t.Errorf("second event = %+v, want the tool call", got[1].Event)
	}
}

// chat_response ends the turn: it is published, then the run stops being
// mirrored so late bus noise doesn't leak onto the wire.
func TestChatStreamStopsAtTerminalEvent(t *testing.T) {
	s, sent := newTestStream()
	s.Track("run1", "sess1")

	s.handle(domain.MonitorEvent{Type: "chat_response", State: "final", RunID: "run1", Summary: "done"})
	s.handle(domain.MonitorEvent{Type: "assistant_delta", RunID: "run1", Summary: "late"})
	s.flushAll()

	got := sent()
	if len(got) != 1 {
		t.Fatalf("want only the terminal event, got %d: %+v", len(got), got)
	}
	if got[0].Event.Type != "chat_response" {
		t.Errorf("event = %+v", got[0].Event)
	}

	s.mu.Lock()
	_, still := s.runs["run1"]
	s.mu.Unlock()
	if still {
		t.Error("run still tracked after its terminal event")
	}
}

// OpenClaw pushes chat_response repeatedly while a reply streams in — state
// "delta"/"partial" chunks before the terminal one. Ending the mirror on the
// FIRST chat_response regardless of state truncates every reply to its first
// chunk; only state "complete"/"final"/"error" may end it (must match the web
// monitor's own reducer — ChatSection.tsx).
func TestChatStreamChatResponseNotTerminalUntilFinalState(t *testing.T) {
	s, sent := newTestStream()
	s.Track("run1", "sess1")

	s.handle(domain.MonitorEvent{Type: "chat_response", State: "delta", RunID: "run1", Summary: "Why"})
	s.handle(domain.MonitorEvent{Type: "chat_response", State: "delta", RunID: "run1", Summary: "Why don't"})
	s.handle(domain.MonitorEvent{Type: "chat_response", State: "final", RunID: "run1", Summary: "Why don't scientists trust atoms?"})

	s.mu.Lock()
	_, still := s.runs["run1"]
	s.mu.Unlock()
	if still {
		t.Error("run still tracked after its final chat_response")
	}

	got := sent()
	if len(got) != 3 {
		t.Fatalf("want all 3 chat_response chunks relayed, got %d: %+v", len(got), got)
	}
	last := got[len(got)-1]
	if last.Event.Summary != "Why don't scientists trust atoms?" {
		t.Errorf("last relayed event = %+v, want the final chunk", last.Event)
	}
}

// A turn that dies without a terminal event must not be tracked forever.
func TestChatStreamSweepsExpiredRuns(t *testing.T) {
	s, _ := newTestStream()
	s.Track("stale", "sess1")
	s.Track("fresh", "sess2")

	s.mu.Lock()
	s.runs["stale"].started = time.Now().Add(-2 * runTTL)
	s.mu.Unlock()

	s.sweep()

	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.runs["stale"]; ok {
		t.Error("expired run survived the sweep")
	}
	if _, ok := s.runs["fresh"]; !ok {
		t.Error("sweep removed a live run")
	}
}

// The bus puts delta text on Summary; the flow_event shape nests it under
// detail.text. Both must read.
func TestDeltaText(t *testing.T) {
	cases := []struct {
		name string
		evt  domain.MonitorEvent
		want string
	}{
		{"summary", domain.MonitorEvent{Summary: "hi"}, "hi"},
		{"detail", domain.MonitorEvent{Detail: map[string]any{"text": "hi"}}, "hi"},
		{"summary wins", domain.MonitorEvent{Summary: "a", Detail: map[string]any{"text": "b"}}, "a"},
		{"neither", domain.MonitorEvent{Detail: map[string]any{"other": 1}}, ""},
	}
	for _, tc := range cases {
		if got := deltaText(tc.evt); got != tc.want {
			t.Errorf("%s: got %q, want %q", tc.name, got, tc.want)
		}
	}
}
