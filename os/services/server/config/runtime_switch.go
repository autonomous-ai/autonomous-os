package config

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// runtimeSwitchMarkerPath records an in-flight agent-runtime switch so a crash
// between switch-runtime landing (NEW unit active, OLD stopped) and
// AgentRuntime being persisted to config.json — e.g. os-server killed by a
// concurrent bootstrap OTA restart — can be healed on the next boot instead of
// leaving config.json pointing at a backend that isn't actually running.
// Written by device.Service.UpdateAgentRuntime right before it runs
// switch-runtime (see internal/device/service.go), so it is on disk for the
// whole duration of the switch, not just a narrow race window.
var runtimeSwitchMarkerPath = "/var/lib/os-server/runtime_switch.json"

// runtimeUnitDeclDir mirrors switch_runtime.sh's unit_for(): an installer may
// declare a non-default systemd unit name for its backend in
// <runtimeUnitDeclDir>/<name>/service (e.g. hermes -> hermes-gateway).
var runtimeUnitDeclDir = "/usr/local/lib/os-runtimes"

type runtimeSwitchMarker struct {
	From string `json:"from"`
	To   string `json:"to"`
}

// WriteRuntimeSwitchMarker records an in-flight switch from -> to.
func WriteRuntimeSwitchMarker(from, to string) error {
	data, err := json.Marshal(runtimeSwitchMarker{From: from, To: to})
	if err != nil {
		return fmt.Errorf("marshal runtime switch marker: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(runtimeSwitchMarkerPath), 0755); err != nil {
		return fmt.Errorf("create runtime switch marker dir: %w", err)
	}
	if err := os.WriteFile(runtimeSwitchMarkerPath, data, 0600); err != nil {
		return fmt.Errorf("write runtime switch marker: %w", err)
	}
	return nil
}

// ClearRuntimeSwitchMarker removes the marker once the switch's outcome is
// fully settled (persisted successfully, or failed/rolled back). Missing file
// is not an error.
func ClearRuntimeSwitchMarker() error {
	if err := os.Remove(runtimeSwitchMarkerPath); err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("remove runtime switch marker: %w", err)
	}
	return nil
}

func readRuntimeSwitchMarker() (*runtimeSwitchMarker, error) {
	data, err := os.ReadFile(runtimeSwitchMarkerPath)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("read runtime switch marker: %w", err)
	}
	var m runtimeSwitchMarker
	if err := json.Unmarshal(data, &m); err != nil {
		return nil, fmt.Errorf("parse runtime switch marker: %w", err)
	}
	return &m, nil
}

// runtimeUnitName resolves the systemd unit name for a backend, mirroring
// switch_runtime.sh's unit_for().
func runtimeUnitName(runtime string) string {
	f := filepath.Join(runtimeUnitDeclDir, runtime, "service")
	data, err := os.ReadFile(f)
	if err != nil {
		return runtime
	}
	name := strings.TrimSpace(string(data))
	if name == "" {
		return runtime
	}
	return name
}

// systemdUnitActive is the production isActive check passed to
// reconcileRuntimeSwitchOnBoot.
func systemdUnitActive(unit string) bool {
	return exec.Command("systemctl", "is-active", "--quiet", unit+".service").Run() == nil
}

// reconcileRuntimeSwitchOnBoot heals a switch that crashed between
// switch-runtime landing and AgentRuntime being persisted. isActive is
// injected for testability; production callers pass systemdUnitActive.
//
// Must run before anything resolves the agent gateway from cfg.AgentRuntime
// (internal/agent/factory.go) — that's why it's called from ProvideConfig,
// the first provider in the wire chain, rather than from the later
// startup-sequence reconciles (persona/config/channel/mcp), which react to a
// gateway that would already be wired to the wrong backend by the time they run.
func (c *Config) reconcileRuntimeSwitchOnBoot(isActive func(unit string) bool) {
	marker, err := readRuntimeSwitchMarker()
	if err != nil {
		slog.Warn("runtime switch marker unreadable, leaving as-is", "component", "config", "error", err)
		return
	}
	if marker == nil {
		return
	}

	if isActive(runtimeUnitName(marker.To)) {
		slog.Warn("runtime switch marker found + target unit active — switch landed but was never persisted (likely crash mid-switch); healing config",
			"component", "config", "from", marker.From, "to", marker.To)
		if err := c.WithLockSave(func(c *Config) { c.AgentRuntime = marker.To }); err != nil {
			slog.Error("runtime switch heal: save config failed, marker left for next boot retry",
				"component", "config", "error", err)
			return
		}
	} else {
		slog.Warn("runtime switch marker found but target unit not active — switch did not land (rolled back or crashed earlier); leaving config as-is",
			"component", "config", "from", marker.From, "to", marker.To)
	}

	if err := ClearRuntimeSwitchMarker(); err != nil {
		slog.Warn("clear runtime switch marker failed", "component", "config", "error", err)
	}
}
