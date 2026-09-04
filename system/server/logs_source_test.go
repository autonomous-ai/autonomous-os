package server

import (
	"testing"

	"go.autonomous.ai/os/system/server/config"
)

// The web UI's log tabs must keep resolving to the board's files/units with no
// env set — the three sources that became env-driven are the ones a laptop can
// actually serve, and getting their defaults wrong would blank the tabs on a
// real device instead.
func TestResolveLogSourceBoardDefaults(t *testing.T) {
	for _, k := range []string{"OS_LOG_FILE", "OS_HAL_LOG_FILE", "OS_AGENT_BRIDGE_LOG"} {
		t.Setenv(k, "")
	}
	s := &Server{config: &config.Config{AgentRuntime: "codex"}}
	want := map[string]string{
		"hal":              "/var/log/hal/server.log",
		"os-server":        "/var/log/os-server.log",
		"bootstrap":        "journal:bootstrap.service",
		"buddy":            "/var/log/claude-desktop-buddy.log",
		"openclaw":         "journal:codex.service", // runtime=codex → Agent tab follows it
		"openclaw-service": "journal:codex.service",
		"codex":            "journal:codex.service",
	}
	for src, w := range want {
		got, ok := s.resolveLogSource(src)
		if !ok || got != w {
			t.Errorf("%s: got (%q, %v), want (%q, true)", src, got, ok, w)
		}
	}
}

// Off-device the same tabs follow the env, so the UI reads the files the dev
// targets actually write. Sources with no laptop equivalent stay unchanged.
func TestResolveLogSourceOffDevice(t *testing.T) {
	t.Setenv("OS_LOG_FILE", "/tmp/os/os-server.log")
	t.Setenv("OS_HAL_LOG_FILE", "/tmp/sim/log/server.log")
	t.Setenv("OS_AGENT_BRIDGE_LOG", "/tmp/os/codex-gatewayd.log")
	s := &Server{config: &config.Config{AgentRuntime: "codex"}}
	want := map[string]string{
		"hal":              "/tmp/sim/log/server.log",
		"os-server":        "/tmp/os/os-server.log",
		"openclaw":         "/tmp/os/codex-gatewayd.log",
		"openclaw-service": "/tmp/os/codex-gatewayd.log",
		"bootstrap":        "journal:bootstrap.service",         // no laptop equivalent
		"buddy":            "/var/log/claude-desktop-buddy.log", // no laptop equivalent
	}
	for src, w := range want {
		got, _ := s.resolveLogSource(src)
		if got != w {
			t.Errorf("%s: got %q, want %q", src, got, w)
		}
	}
}
