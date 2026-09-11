package ambient

import (
	"context"
	"go.autonomous.ai/os/system/domain"
	"testing"
	"time"
)

func TestEnvironmentObservationDoesNotWakeOrCountAsInteraction(t *testing.T) {
	s := &Service{sleeping: true, paused: true}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	events := make(chan domain.MonitorEvent)
	done := make(chan struct{})
	go func() { defer close(done); s.watchInteractions(ctx, events) }()
	events <- domain.MonitorEvent{Type: "sensing_input", Detail: map[string]any{"type": "environment.update"}}
	// The next receive confirms the observation was processed.
	events <- domain.MonitorEvent{Type: "test_barrier"}
	s.mu.Lock()
	sleeping, last := s.sleeping, s.lastInteraction
	s.mu.Unlock()
	if !sleeping || !last.IsZero() {
		t.Fatal("environment observation woke device or changed interaction time")
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("watcher did not stop")
	}
}
