package config

import (
	"os"
	"path/filepath"
	"testing"
)

// withTempMarkerPath points runtimeSwitchMarkerPath at a temp file for the
// duration of the test and restores it afterward.
func withTempMarkerPath(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "runtime_switch.json")
	orig := runtimeSwitchMarkerPath
	runtimeSwitchMarkerPath = path
	t.Cleanup(func() { runtimeSwitchMarkerPath = orig })
	return path
}

func TestRuntimeSwitchMarker_WriteReadClearRoundTrip(t *testing.T) {
	withTempMarkerPath(t)

	if err := WriteRuntimeSwitchMarker("openclaw", "claudecode"); err != nil {
		t.Fatalf("WriteRuntimeSwitchMarker: %v", err)
	}

	marker, err := readRuntimeSwitchMarker()
	if err != nil {
		t.Fatalf("readRuntimeSwitchMarker: %v", err)
	}
	if marker == nil {
		t.Fatal("readRuntimeSwitchMarker: got nil marker, want one")
	}
	if marker.From != "openclaw" || marker.To != "claudecode" {
		t.Errorf("marker = %+v, want from=openclaw to=claudecode", marker)
	}

	if err := ClearRuntimeSwitchMarker(); err != nil {
		t.Fatalf("ClearRuntimeSwitchMarker: %v", err)
	}
	marker, err = readRuntimeSwitchMarker()
	if err != nil {
		t.Fatalf("readRuntimeSwitchMarker after clear: %v", err)
	}
	if marker != nil {
		t.Errorf("marker after clear = %+v, want nil", marker)
	}
}

func TestRuntimeSwitchMarker_ClearWhenAbsentIsNoop(t *testing.T) {
	withTempMarkerPath(t)

	if err := ClearRuntimeSwitchMarker(); err != nil {
		t.Fatalf("ClearRuntimeSwitchMarker on missing file: %v", err)
	}
}

func TestReconcileRuntimeSwitchOnBoot_NoMarker_NoOp(t *testing.T) {
	withTempMarkerPath(t)
	dir := t.TempDir()
	origPath := configPath
	configPath = filepath.Join(dir, "config.json")
	defer func() { configPath = origPath }()

	c := &Config{AgentRuntime: "openclaw"}
	calls := 0
	c.reconcileRuntimeSwitchOnBoot(func(unit string) bool {
		calls++
		return true
	})

	if c.AgentRuntime != "openclaw" {
		t.Errorf("AgentRuntime = %q, want unchanged openclaw", c.AgentRuntime)
	}
	if calls != 0 {
		t.Errorf("isActive called %d times, want 0 (no marker => no systemd check)", calls)
	}
}

// This is the exact Tony scenario: switch-runtime landed (claudecode active,
// openclaw stopped) but os-server was killed (bootstrap OTA race) before it
// could persist AgentRuntime. On the next boot, the marker + actually-active
// unit must heal config.AgentRuntime to the target that is really running.
func TestReconcileRuntimeSwitchOnBoot_MarkerAndTargetActive_HealsConfig(t *testing.T) {
	withTempMarkerPath(t)
	dir := t.TempDir()
	origPath := configPath
	configPath = filepath.Join(dir, "config.json")
	defer func() { configPath = origPath }()

	if err := WriteRuntimeSwitchMarker("openclaw", "claudecode"); err != nil {
		t.Fatalf("WriteRuntimeSwitchMarker: %v", err)
	}

	c := &Config{AgentRuntime: "openclaw"}
	c.reconcileRuntimeSwitchOnBoot(func(unit string) bool {
		return unit == "claudecode"
	})

	if c.AgentRuntime != "claudecode" {
		t.Errorf("AgentRuntime = %q, want healed to claudecode", c.AgentRuntime)
	}
	data, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatalf("read config.json: %v", err)
	}
	if !contains(string(data), `"claudecode"`) {
		t.Errorf("config.json not persisted with healed runtime:\n%s", data)
	}
	marker, err := readRuntimeSwitchMarker()
	if err != nil {
		t.Fatalf("readRuntimeSwitchMarker after reconcile: %v", err)
	}
	if marker != nil {
		t.Errorf("marker left on disk after successful heal = %+v, want cleared", marker)
	}
}

// Switch never actually landed (rolled back by switch_runtime.sh's trap, or
// os-server crashed before switch-runtime even started) — target unit is not
// active, so config must NOT be changed, only the stale marker cleaned up.
func TestReconcileRuntimeSwitchOnBoot_MarkerButTargetNotActive_LeavesConfigUnchanged(t *testing.T) {
	withTempMarkerPath(t)
	dir := t.TempDir()
	origPath := configPath
	configPath = filepath.Join(dir, "config.json")
	defer func() { configPath = origPath }()

	if err := WriteRuntimeSwitchMarker("openclaw", "claudecode"); err != nil {
		t.Fatalf("WriteRuntimeSwitchMarker: %v", err)
	}

	c := &Config{AgentRuntime: "openclaw"}
	c.reconcileRuntimeSwitchOnBoot(func(unit string) bool {
		return false // neither claudecode nor openclaw reported active
	})

	if c.AgentRuntime != "openclaw" {
		t.Errorf("AgentRuntime = %q, want unchanged openclaw", c.AgentRuntime)
	}
	marker, err := readRuntimeSwitchMarker()
	if err != nil {
		t.Fatalf("readRuntimeSwitchMarker after reconcile: %v", err)
	}
	if marker != nil {
		t.Errorf("marker left on disk after reconcile = %+v, want cleared", marker)
	}
}

func TestRuntimeUnitName_DefaultsToRuntimeNameWhenNoDeclFile(t *testing.T) {
	dir := t.TempDir()
	orig := runtimeUnitDeclDir
	runtimeUnitDeclDir = dir
	defer func() { runtimeUnitDeclDir = orig }()

	if got := runtimeUnitName("claudecode"); got != "claudecode" {
		t.Errorf("runtimeUnitName(claudecode) = %q, want claudecode (no decl file)", got)
	}
}

func TestRuntimeUnitName_ReadsDeclaredUnitOverride(t *testing.T) {
	dir := t.TempDir()
	orig := runtimeUnitDeclDir
	runtimeUnitDeclDir = dir
	defer func() { runtimeUnitDeclDir = orig }()

	hermesDir := filepath.Join(dir, "hermes")
	if err := os.MkdirAll(hermesDir, 0755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(hermesDir, "service"), []byte("hermes-gateway\n"), 0644); err != nil {
		t.Fatalf("write service decl: %v", err)
	}

	if got := runtimeUnitName("hermes"); got != "hermes-gateway" {
		t.Errorf("runtimeUnitName(hermes) = %q, want hermes-gateway (declared override)", got)
	}
}
