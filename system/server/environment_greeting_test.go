package server

import (
	"errors"
	"testing"

	"go.autonomous.ai/os/system/environment"
)

func TestWakeGreetingWithoutEnvironmentDoesNotWait(t *testing.T) {
	for _, allowed := range []bool{true, false} {
		startup := environment.NewStartupCoordinator()
		calls := 0
		err := sendWakeGreetingWithEnvironment("wake now", startup, allowed, func(prompt string) (string, error) {
			calls++
			if prompt != "wake now" {
				t.Fatalf("cold greeting changed: %q", prompt)
			}
			return "run-1", nil
		})
		if err != nil || calls != 1 {
			t.Fatalf("greeting calls=%d error=%v", calls, err)
		}
	}
}

func TestWakeGreetingPropagatesGatewayFailure(t *testing.T) {
	want := errors.New("gateway unavailable")
	err := sendWakeGreetingWithEnvironment("wake", environment.NewStartupCoordinator(), true, func(string) (string, error) {
		return "", want
	})
	if !errors.Is(err, want) {
		t.Fatalf("got %v, want %v", err, want)
	}
}
