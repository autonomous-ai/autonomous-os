package openclaw

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"go.autonomous.ai/os/server/config"
)

// repoDevicesDir resolves the committed devices/ tree from the test working dir
// (os/services/internal/openclaw → repo root is four levels up).
func repoDevicesDir(t *testing.T) string {
	t.Helper()
	wd, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	return filepath.Join(wd, "..", "..", "..", "..", "devices")
}

func soulFor(t *testing.T, deviceType string) ([]byte, bool) {
	t.Helper()
	t.Setenv("DEVICES_DIR", repoDevicesDir(t))
	s := &Service{config: &config.Config{DeviceType: deviceType}}
	return s.deviceSoulCore()
}

// Lamp ships its own persona → we override the gateway default with it.
func TestDeviceSoulCore_LampHasOwnSoul(t *testing.T) {
	content, has := soulFor(t, "lamp")
	if !has {
		t.Fatal("lamp must resolve a SOUL.md")
	}
	if !strings.Contains(string(content), "You are **Lamp**") {
		t.Errorf("lamp soul missing persona text; got start %q", head(content))
	}
}

// Intern is a body with no persona → no SOUL.md → we must NOT override; the
// agentic runtime (OpenClaw) keeps its own default soul.
func TestDeviceSoulCore_InternHasNoSoul(t *testing.T) {
	if _, has := soulFor(t, "intern"); has {
		t.Error("intern declares no SOUL.md — deviceSoulCore must return hasSoul=false")
	}
}

// A different body (dog) gets its own soul from the same binary.
func TestDeviceSoulCore_DogHasOwnSoul(t *testing.T) {
	content, has := soulFor(t, "unitree-go2w")
	if !has {
		t.Fatal("unitree-go2w must resolve a SOUL.md")
	}
	if !strings.Contains(string(content), "Unitree Go2-W") {
		t.Errorf("dog soul missing its persona; got start %q", head(content))
	}
}

// Empty device_type falls back to "lamp" (DeviceTypeOrDefault).
func TestDeviceSoulCore_EmptyTypeDefaultsToLamp(t *testing.T) {
	content, has := soulFor(t, "")
	if !has || !strings.Contains(string(content), "You are **Lamp**") {
		t.Errorf("empty device_type should resolve to the lamp soul")
	}
}

// An unknown device type has no profile on disk → no soul, no override.
func TestDeviceSoulCore_UnknownTypeHasNoSoul(t *testing.T) {
	if _, has := soulFor(t, "does-not-exist"); has {
		t.Error("unknown device type must return hasSoul=false (no embedded fallback)")
	}
}

func head(b []byte) string {
	const n = 48
	if len(b) < n {
		return string(b)
	}
	return string(b[:n])
}
