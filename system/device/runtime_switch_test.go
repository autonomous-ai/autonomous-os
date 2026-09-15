package device

import (
	"errors"
	"os"
	"path/filepath"
	"testing"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

func TestReserveAgentRuntimeSwitchRejectsConcurrentSwitch(t *testing.T) {
	svc := &Service{}
	first, err := svc.ReserveAgentRuntimeSwitch(domain.AgentRuntimeSetData{Runtime: "invalid"})
	if err != nil {
		t.Fatalf("reserve first switch: %v", err)
	}

	if _, err := svc.ReserveAgentRuntimeSwitch(domain.AgentRuntimeSetData{Runtime: "openclaw"}); !errors.Is(err, ErrAgentRuntimeSwitchInProgress) {
		t.Fatalf("reserve concurrent switch error = %v, want ErrAgentRuntimeSwitchInProgress", err)
	}

	if _, err := first(); err == nil {
		t.Fatal("first switch with invalid runtime unexpectedly succeeded")
	}

	second, err := svc.ReserveAgentRuntimeSwitch(domain.AgentRuntimeSetData{Runtime: "invalid"})
	if err != nil {
		t.Fatalf("reserve switch after release: %v", err)
	}
	if _, err := second(); err == nil {
		t.Fatal("second switch with invalid runtime unexpectedly succeeded")
	}
}

func TestRestartForAgentRuntimeRejectsSavedInternSelection(t *testing.T) {
	svc := &Service{config: &config.Config{AgentRuntime: domain.AgentRuntimeIntern}}
	if err := svc.RestartForAgentRuntime(); !errors.Is(err, domain.ErrNotSupportedByRuntime) {
		t.Fatalf("restart with saved Intern selection = %v, want ErrNotSupportedByRuntime", err)
	}
}

func TestRestartForAgentRuntimeNilConfigFixtureIsSafe(t *testing.T) {
	dir := t.TempDir()
	stub := filepath.Join(dir, "systemd-run")
	if err := os.WriteFile(stub, []byte("#!/bin/sh\nexit 0\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir)

	if err := (&Service{}).RestartForAgentRuntime(); err != nil {
		t.Fatalf("restart with nil config = %v, want nil from isolated launcher", err)
	}
}
