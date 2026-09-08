package gatewayd

import (
	"os"
	"testing"
)

// TestConfigFromEnvHomes pins both halves of the home contract: unset env is the
// board, and OS_AGENT_HOME relocates the claude child's HOME (hence ~/.claude)
// together with the backend state dir. CLAUDECODE_HOME still overrides the
// latter on its own.
func TestConfigFromEnvHomes(t *testing.T) {
	for _, k := range []string{
		"OS_AGENT_HOME", "CLAUDECODE_HOME", "CLAUDECODE_WORKSPACE", "CLAUDECODE_ENV_FILE",
		"CLAUDECODE_SESSION_FILE", "CLAUDECODE_PORT", "CLAUDECODE_WS_TOKEN", "CLAUDECODE_BIN",
	} {
		t.Setenv(k, "")
		os.Unsetenv(k)
	}
	c := configFromEnv()
	for _, tc := range []struct{ name, got, want string }{
		{"Home", c.Home, "/root"},
		{"Workspace", c.Workspace, "/root/.claudecode/workspace"},
		{"EnvFile", c.EnvFile, "/root/.claudecode/.env"},
		{"SessionFile", c.SessionFile, "/root/.claudecode/session.json"},
		{"Port", c.Port, "18791"},
		{"ClaudeBin", c.ClaudeBin, "claude"},
	} {
		if tc.got != tc.want {
			t.Errorf("device default %s = %q, want %q", tc.name, tc.got, tc.want)
		}
	}

	t.Setenv("OS_AGENT_HOME", "/tmp/agent")
	c = configFromEnv()
	if c.Home != "/tmp/agent" || c.Workspace != "/tmp/agent/.claudecode/workspace" || c.EnvFile != "/tmp/agent/.claudecode/.env" {
		t.Errorf("OS_AGENT_HOME did not relocate: %+v", c)
	}

	t.Setenv("CLAUDECODE_HOME", "/tmp/state")
	c = configFromEnv()
	if c.Home != "/tmp/agent" || c.Workspace != "/tmp/state/workspace" {
		t.Errorf("CLAUDECODE_HOME must move only the state dir: %+v", c)
	}
}
