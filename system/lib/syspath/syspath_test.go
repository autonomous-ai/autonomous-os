package syspath

import "testing"

// The device (OrangePi) contract: every accessor must return the literal it
// replaced when its env var is unset. envOr treats "" as unset, so setting each
// var to "" makes this run unconditionally — a dev who exports CODEX_HOME in
// their shell profile still gets the guard instead of a silent skip.
func TestDeviceDefaults(t *testing.T) {
	cases := []struct {
		env  string
		got  func() string
		want string
	}{
		{"CODEX_HOME", CodexHome, "/root/.codex"},
		{"CODEX_PORT", CodexPort, "18792"},
		{"CODEX_WS_TOKEN", CodexWSToken, "autonomous_codex_token"},
		{"OS_AGENT_HOME", AgentHome, "/root"},
		{"OS_AGENT_STATE_PATH", AgentStatePath, "/root/config/agent_state.json"},
		{"OS_BOOTSTRAP_CONFIG", BootstrapConfig, "/root/config/bootstrap.json"},
		{"OS_LOG_FILE", LogFile, "/var/log/os-server.log"},
	}
	for _, c := range cases {
		t.Setenv(c.env, "")
		if got := c.got(); got != c.want {
			t.Errorf("%s unset: got %q, want %q", c.env, got, c.want)
		}
		t.Setenv(c.env, "/tmp/override")
		if got := c.got(); got != "/tmp/override" {
			t.Errorf("%s set: got %q, want the override", c.env, got)
		}
	}
}

// AgentRuntimeHome is the one accessor that is not a bare env lookup, so the
// device contract has to be asserted per runtime: on a board every runtime must
// still resolve to /root/.<runtime> with nothing set. Codex additionally has to
// track CODEX_HOME rather than OS_AGENT_HOME — off-device those two point at
// different places (a real codex install vs. throwaway state), and composing
// them would name a directory HAL never writes a snapshot to.
func TestAgentRuntimeHome(t *testing.T) {
	t.Setenv("OS_AGENT_HOME", "")
	t.Setenv("CODEX_HOME", "")
	for _, rt := range []string{"codex", "openclaw", "hermes", "picoclaw", "claudecode", "opencode"} {
		if got, want := AgentRuntimeHome(rt), "/root/."+rt; got != want {
			t.Errorf("%s unset env: got %q, want %q", rt, got, want)
		}
	}

	t.Setenv("CODEX_HOME", "/Users/dev/.codex")
	t.Setenv("OS_AGENT_HOME", "/tmp/state")
	if got, want := AgentRuntimeHome("codex"), "/Users/dev/.codex"; got != want {
		t.Errorf("codex: got %q, want %q — must follow CODEX_HOME, not OS_AGENT_HOME", got, want)
	}
	if got, want := AgentRuntimeHome("openclaw"), "/tmp/state/.openclaw"; got != want {
		t.Errorf("openclaw: got %q, want %q", got, want)
	}
}

// The board must keep reporting to the backend: unset (and any value other than
// the explicit "off") has to stay on, or a fleet upgrade would silently take
// every device off its uplink. Only `make os-dev`'s explicit "off" disables it.
func TestBackendUplink(t *testing.T) {
	for _, c := range []struct {
		env  string
		want bool
	}{
		{"", true},         // unset — the device default
		{"on", true},       // what a deliberate off-device opt-in sets
		{"off", false},     // what make os-dev sets
		{"anything", true}, // a typo must fail SAFE for the board, i.e. stay on
	} {
		t.Setenv("OS_BACKEND_UPLINK", c.env)
		if got := BackendUplink(); got != c.want {
			t.Errorf("OS_BACKEND_UPLINK=%q: got %v, want %v", c.env, got, c.want)
		}
	}
}
