package codex

import (
	"encoding/json"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
)

// newStuckService builds a service whose busy flag is already older than the
// TTL, with a turn in flight.
func newStuckService(t *testing.T, runID string) (*CodexService, *[]domain.WSEvent) {
	t.Helper()
	s := &CodexService{}
	s.activeTurn.Store(true)
	s.busySince.Store(time.Now().Add(-24 * time.Hour).UnixMilli())
	if runID != "" {
		s.setCurrentRunID(runID)
	}
	var got []domain.WSEvent
	s.wsDispatch.Store(dispatchFn(func(e domain.WSEvent) { got = append(got, e) }))
	return s, &got
}

// The bug: the TTL used to clear the busy flag but leave currentRunID set, so
// the NEXT turn's ensureTurnStarted returned early and every frame of the new
// turn was attributed to the dead run — the new chat then hung with no reply.
func TestBusyTTLClearsTheOrphanedRunID(t *testing.T) {
	s, _ := newStuckService(t, "device-chat-7-123")
	if s.IsBusy() {
		t.Fatal("an expired busy flag must not report busy")
	}
	if got := s.getCurrentRunID(); got != "" {
		t.Fatalf("the dead run id must be cleared, still have %q", got)
	}
}

// Clearing alone is not enough either: the browser is waiting on that run id
// and would sit on a pending bubble until its own deadline.
func TestBusyTTLTellsTheClientTheTurnDied(t *testing.T) {
	s, got := newStuckService(t, "device-chat-7-123")
	_ = s.IsBusy()

	if len(*got) != 1 {
		t.Fatalf("expected exactly one lifecycle event, got %d", len(*got))
	}
	var payload struct {
		RunID string `json:"runId"`
		Data  struct {
			Phase string `json:"phase"`
			Error string `json:"error"`
		} `json:"data"`
	}
	if err := json.Unmarshal((*got)[0].Payload, &payload); err != nil {
		t.Fatalf("payload is not valid JSON: %v", err)
	}
	if payload.RunID != "device-chat-7-123" {
		t.Fatalf("the error must carry the run the client is waiting on, got %q", payload.RunID)
	}
	if payload.Data.Phase != "error" {
		t.Fatalf("expected a lifecycle error, got phase %q", payload.Data.Phase)
	}
	if payload.Data.Error == "" {
		t.Fatal("the client needs a reason to show instead of a spinner")
	}
}

// A stale busy flag with no turn behind it must not invent a run to fail.
func TestBusyTTLWithNoTurnEmitsNothing(t *testing.T) {
	s, got := newStuckService(t, "")
	_ = s.IsBusy()
	if len(*got) != 0 {
		t.Fatalf("no turn was in flight, nothing should be dispatched, got %v", *got)
	}
}

// A turn still inside its TTL is left completely alone.
func TestBusyTTLDoesNotTouchALiveTurn(t *testing.T) {
	s, got := newStuckService(t, "device-chat-8-456")
	s.busySince.Store(time.Now().UnixMilli())
	if !s.IsBusy() {
		t.Fatal("a fresh turn must still report busy")
	}
	if got2 := s.getCurrentRunID(); got2 != "device-chat-8-456" {
		t.Fatalf("a live turn must keep its run id, got %q", got2)
	}
	if len(*got) != 0 {
		t.Fatalf("a live turn must not be failed, got %v", *got)
	}
}
