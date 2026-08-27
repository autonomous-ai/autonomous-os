package system

import (
	"testing"
	"time"
)

func TestTriggerPowerDispatchesOnceAfterTheAcknowledgementDelay(t *testing.T) {
	powerMu.Lock()
	originalPending := powerPending
	powerPending = ""
	powerMu.Unlock()
	originalDelay := powerActionDelay
	originalRequest := powerRequest
	t.Cleanup(func() {
		powerMu.Lock()
		powerPending = originalPending
		powerMu.Unlock()
		powerActionDelay = originalDelay
		powerRequest = originalRequest
	})

	powerActionDelay = 0
	called := make(chan powerAction, 1)
	powerRequest = func(action powerAction) error {
		called <- action
		return nil
	}

	started, reason := TriggerReboot()
	if !started || reason != "" {
		t.Fatalf("TriggerReboot() = (%v, %q), want (true, \"\")", started, reason)
	}

	select {
	case got := <-called:
		if got != powerActionReboot {
			t.Fatalf("requested %q, want %q", got, powerActionReboot)
		}
	case <-time.After(time.Second):
		t.Fatal("power request was not dispatched")
	}
}

func TestTriggerPowerRejectsASecondPendingAction(t *testing.T) {
	powerMu.Lock()
	originalPending := powerPending
	powerPending = powerActionShutdown
	powerMu.Unlock()
	t.Cleanup(func() {
		powerMu.Lock()
		powerPending = originalPending
		powerMu.Unlock()
	})

	started, reason := TriggerReboot()
	if started || reason == "" {
		t.Fatalf("TriggerReboot() = (%v, %q), want rejection with a reason", started, reason)
	}
}
