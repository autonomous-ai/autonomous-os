package server

import (
	"testing"
	"time"

	"go.autonomous.ai/os/system/lib/hal"
)

// waitHALReady must give up on its own deadline rather than block startup
// forever when HAL never comes up. hal.BaseURL is a const (loopback :5001), so
// this exercises the real unreachable path instead of a stub; skip when a HAL
// happens to be running on the dev machine.
func TestWaitHALReadyGivesUpAtDeadline(t *testing.T) {
	if _, err := hal.GetHealth(); err == nil {
		t.Skip("a live HAL is listening on this machine")
	}
	const timeout = 1500 * time.Millisecond
	start := time.Now()
	if waitHALReady(timeout) {
		t.Fatal("waitHALReady = true with no HAL listening")
	}
	if elapsed := time.Since(start); elapsed < timeout || elapsed > timeout+3*time.Second {
		t.Fatalf("waitHALReady returned after %v, want ~%v", elapsed, timeout)
	}
}
