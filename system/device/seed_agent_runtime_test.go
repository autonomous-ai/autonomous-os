package device

import (
	"os"
	"path/filepath"
	"testing"

	"go.autonomous.ai/os/system/server/config"
)

// setFRDefaultAgentPath points frDefaultAgentPath at a temp file for the
// duration of the test and restores the real path after.
func setFRDefaultAgentPath(t *testing.T, content string) {
	t.Helper()
	orig := frDefaultAgentPath
	t.Cleanup(func() { frDefaultAgentPath = orig })

	path := filepath.Join(t.TempDir(), "f_r_default_agent")
	if content != "" {
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			t.Fatalf("write f_r_default_agent fixture: %v", err)
		}
	}
	frDefaultAgentPath = path
}

func TestSeedAgentRuntimeFromGateway_PrefersFRDefaultOverDeviceMD(t *testing.T) {
	t.Chdir(t.TempDir()) // config.Save() writes config/config.json relative to cwd
	t.Setenv("DEVICE_TYPE", "intern-v2")
	writeDeviceMD(t, "intern-v2", `---
schema: autonomous.device.v1
gateway:
  default: openclaw
---
`)
	setFRDefaultAgentPath(t, "hermes\n")

	cfg := &config.Config{}
	SeedAgentRuntimeFromGateway(cfg)

	if cfg.AgentRuntime != "hermes" {
		t.Fatalf("AgentRuntime = %q, want %q (f_r_default_agent should win over DEVICE.md gateway.default)", cfg.AgentRuntime, "hermes")
	}
}

func TestSeedAgentRuntimeFromGateway_FallsBackToDeviceMDWhenFRDefaultAbsent(t *testing.T) {
	t.Chdir(t.TempDir())
	t.Setenv("DEVICE_TYPE", "intern-v2")
	writeDeviceMD(t, "intern-v2", `---
schema: autonomous.device.v1
gateway:
  default: openclaw
---
`)
	setFRDefaultAgentPath(t, "") // file absent — most builds

	cfg := &config.Config{}
	SeedAgentRuntimeFromGateway(cfg)

	if cfg.AgentRuntime != "openclaw" {
		t.Fatalf("AgentRuntime = %q, want %q (fallback to DEVICE.md gateway.default)", cfg.AgentRuntime, "openclaw")
	}
}

func TestSeedAgentRuntimeFromGateway_InvalidFRDefaultFallsBackToDeviceMD(t *testing.T) {
	t.Chdir(t.TempDir())
	t.Setenv("DEVICE_TYPE", "intern-v2")
	writeDeviceMD(t, "intern-v2", `---
schema: autonomous.device.v1
gateway:
  default: openclaw
---
`)
	setFRDefaultAgentPath(t, "not-a-real-runtime\n")

	cfg := &config.Config{}
	SeedAgentRuntimeFromGateway(cfg)

	if cfg.AgentRuntime != "openclaw" {
		t.Fatalf("AgentRuntime = %q, want %q (invalid f_r_default_agent should fall back)", cfg.AgentRuntime, "openclaw")
	}
}

func TestSeedAgentRuntimeFromGateway_NoopWhenAlreadySet(t *testing.T) {
	t.Chdir(t.TempDir())
	t.Setenv("DEVICE_TYPE", "intern-v2")
	writeDeviceMD(t, "intern-v2", `---
schema: autonomous.device.v1
gateway:
  default: openclaw
---
`)
	setFRDefaultAgentPath(t, "hermes\n")

	cfg := &config.Config{AgentRuntime: "claudecode"}
	SeedAgentRuntimeFromGateway(cfg)

	if cfg.AgentRuntime != "claudecode" {
		t.Fatalf("AgentRuntime = %q, want unchanged %q (device already owns its runtime)", cfg.AgentRuntime, "claudecode")
	}
}
