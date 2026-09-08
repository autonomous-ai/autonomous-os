package claudecode

import (
	"encoding/json"
	"fmt"
	"testing"

	"go.autonomous.ai/os/system/domain"
)

func TestThreeQueuedTurnsKeepFIFOThroughSuccessAndError(t *testing.T) {
	s := &ClaudeCodeService{}
	for i := 1; i <= 3; i++ {
		s.addPendingRun(fmt.Sprintf("req-%d", i), fmt.Sprintf("run-%d", i))
	}
	for i := 1; i <= 3; i++ {
		var events []domain.WSEvent
		dispatch := func(e domain.WSEvent) { events = append(events, e) }
		raw := `{"type":"result","subtype":"success","result":"done"}`
		if i == 2 {
			raw = `{"type":"result","is_error":true,"result":"tool failed"}`
		}
		s.translateFrame([]byte(raw), dispatch)
		if len(events) < 2 {
			t.Fatalf("turn %d missing terminal", i)
		}
		for _, e := range events {
			var payload map[string]any
			_ = json.Unmarshal(e.Payload, &payload)
			if payload["runId"] != fmt.Sprintf("run-%d", i) {
				t.Fatalf("turn %d misattributed: %s", i, e.Payload)
			}
		}
		s.SetBusy(false)
		if s.activeTurn.Load() != (i < 3) {
			t.Fatalf("turn %d busy gating lost", i)
		}
	}
	if s.hasPendingRuns() {
		t.Fatal("completed queue leaked")
	}
}

func TestDisconnectDropsOnlyTransmittedCorrelation(t *testing.T) {
	s := &ClaudeCodeService{}
	s.addPendingRun("sent", "run-sent")
	s.pendingEvents = []pendingEvent{{fixedRunID: "unsent"}}
	s.setCurrentRunID("active")
	s.clearTurn()
	s.SetBusy(false)
	if s.hasPendingRuns() || s.getCurrentRunID() != "" || s.activeTurn.Load() {
		t.Fatal("phantom transmitted run retained")
	}
	if len(s.pendingEvents) != 1 {
		t.Fatal("unsent user request discarded")
	}
}
