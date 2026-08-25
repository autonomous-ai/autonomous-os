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
