package bootstrap

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"go.autonomous.ai/os/system/bootstrap/state"
	"go.autonomous.ai/os/system/domain"
)

func TestCompareVersions(t *testing.T) {
	cases := []struct {
		a, b string
		want int
	}{
		{"1.2.3", "1.2.3", 0},
		{"1.2.3", "1.2.4", -1},
		{"1.3.0", "1.2.9", 1},
		{"2.0.0", "1.9.9", 1},
		// numeric, not lexical: 27 > 9
		{"2026.5.27", "2026.5.9", 1},
		{"2026.5.9", "2026.5.27", -1},
		// pre-release/build suffix ignored (numeric core only)
		{"1.2.3-rc1", "1.2.3", 0},
		{"1.2.3+build5", "1.2.3", 0},
		// "v" prefix / surrounding text tolerated via semverRe extraction
		{"v1.4.0", "1.4.0", 0},
		// empty / unparseable sorts lowest
		{"", "0.0.1", -1},
		{"", "", 0},
		{"garbage", "1.0.0", -1},
	}
	for _, c := range cases {
		if got := compareVersions(c.a, c.b); got != c.want {
			t.Errorf("compareVersions(%q, %q) = %d, want %d", c.a, c.b, got, c.want)
		}
	}
}

// A component the device does not have must not read as "out of date". Before
// this gate, an absent artifact made detectVersion return "" — which sorts below
// every min_version floor — so the worker announced an update over the speaker,
// lit the OTA LED and tried to install it on every poll, forever.
func TestReconcileSkipsUninstalledComponent(t *testing.T) {
	devicesDir := t.TempDir() // no <type> subdir → the profile is not installed
	t.Setenv("DEVICES_DIR", devicesDir)
	t.Setenv("DEVICE_TYPE", "reachy-mini")

	b := &Bootstrap{state: &state.State{Components: map[string]string{}}}

	// Without the gate this reaches applyUpdate, which execs software-update and
	// returns an error — so a nil error is what proves the skip happened.
	updated, err := b.reconcile(context.Background(), domain.OTAKeyDevice,
		domain.OTAComponent{Version: "9.9.9"})
	if err != nil {
		t.Fatalf("reconcile errored on a component this device does not have: %v", err)
	}
	if updated {
		t.Fatal("reconcile reported an update for a component this device does not have")
	}
	if v, ok := b.state.Components[domain.OTAKeyDevice]; ok {
		t.Fatalf("a skipped component must not be written to state, got %q", v)
	}
}

func TestComponentInstalled(t *testing.T) {
	devicesDir := t.TempDir()
	t.Setenv("DEVICES_DIR", devicesDir)
	t.Setenv("DEVICE_TYPE", "reachy-mini")
	b := &Bootstrap{}

	if b.componentInstalled(domain.OTAKeyDevice) {
		t.Error("device profile reported installed with no profile directory")
	}
	if err := os.Mkdir(filepath.Join(devicesDir, "reachy-mini"), 0o755); err != nil {
		t.Fatal(err)
	}
	if !b.componentInstalled(domain.OTAKeyDevice) {
		t.Error("device profile reported missing although its directory exists")
	}
	// The worker is the bootstrap component: always installed, so it can always
	// self-update.
	if !b.componentInstalled(domain.OTAKeyBootstrap) {
		t.Error("bootstrap must always count as installed")
	}
	// An unresolvable device type must not resolve to some other device's dir.
	t.Setenv("DEVICE_TYPE", "")
	if b.componentInstalled(domain.OTAKeyDevice) {
		t.Error("device profile reported installed with an unresolved device type")
	}
}
