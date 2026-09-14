package sensingmsg

import (
	"strings"
	"testing"
)

func TestEnvironmentReplayRechecksPolicy(t *testing.T) {
	SetEnvironmentReplayAllowed(nil)
	t.Cleanup(func() { SetEnvironmentReplayAllowed(nil) })
	if ReplayAllowed("environment.update") {
		t.Fatal("unconfigured environment must fail closed")
	}
	if !ReplayAllowed("voice") {
		t.Fatal("other event policy must be unchanged")
	}
	sleeping := false
	declared := true
	SetEnvironmentReplayAllowed(func() bool { return declared && !sleeping })
	if !ReplayAllowed("environment.update") {
		t.Fatal("awake declared environment should replay")
	}
	sleeping = true
	if ReplayAllowed("environment.update") {
		t.Fatal("sleep entered while queued must suppress replay")
	}
	sleeping = false
	declared = false
	if ReplayAllowed("environment.update") {
		t.Fatal("capability removed while queued must suppress replay")
	}
}

func TestEnvironmentNeverUsesGuardWrapper(t *testing.T) {
	message := Build("environment.update", `{"observed_at":123}`, "", "[guard-active]")
	if strings.Contains(message, "[guard-active]") || !strings.HasPrefix(message, "[environment:update]") {
		t.Fatalf("environment incorrectly entered guard route: %s", message)
	}
}
