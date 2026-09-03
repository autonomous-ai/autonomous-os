package codex

import (
	"testing"
	"time"
)

// The bug this guards: busyTTL used to be a fixed 5 minutes while the gatewayd
// let a turn run for 10, so every turn slower than 5 minutes tripped the
// "final frame was dropped" path and lost its run id.
func TestBusyTTLOutlivesTheGatewaydTurnTimeout(t *testing.T) {
	t.Setenv("CODEX_TURN_TIMEOUT_S", "2700")
	if got, want := busyTTL(), 45*time.Minute+busyTTLMargin; got != want {
		t.Fatalf("busyTTL = %s, want %s", got, want)
	}
}

func TestBusyTTLFollowsARaisedTurnTimeout(t *testing.T) {
	t.Setenv("CODEX_TURN_TIMEOUT_S", "7200")
	if got := busyTTL(); got <= 2*time.Hour {
		t.Fatalf("busyTTL must stay above the turn timeout, got %s", got)
	}
}

func TestBusyTTLFallsBackWhenEnvIsUnusable(t *testing.T) {
	for _, v := range []string{"", "not-a-number", "0", "-5"} {
		t.Setenv("CODEX_TURN_TIMEOUT_S", v)
		if got, want := busyTTL(), 45*time.Minute+busyTTLMargin; got != want {
			t.Fatalf("CODEX_TURN_TIMEOUT_S=%q: busyTTL = %s, want the default %s", v, got, want)
		}
	}
}
