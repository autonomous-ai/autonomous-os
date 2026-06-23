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
	// DEVICE_TYPE is the primary resolver (env-first); set it explicitly so the
	// test is deterministic regardless of ambient env. Empty → config.json
	// device_type ("" here) → DeviceTypeOrDefault returns "" (no "lamp" fallback).
	t.Setenv("DEVICE_TYPE", deviceType)
	s := &Service{config: &config.Config{}}
	content, has, err := s.deviceSoulCore()
	if err != nil {
		t.Fatalf("deviceSoulCore(%q): %v", deviceType, err)
	}
	return content, has
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

// Intern-v2 is a body with no persona → no soul_ref → we must NOT override; the
// agentic runtime (OpenClaw) keeps its own default soul.
func TestDeviceSoulCore_InternHasNoSoul(t *testing.T) {
	if _, has := soulFor(t, "intern-v2"); has {
		t.Error("intern-v2 declares no soul_ref — deviceSoulCore must return hasSoul=false")
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

// Empty device_type no longer falls back to "lamp" — DeviceTypeOrDefault returns
// "" and resolves no soul (the Serve startup guard fail-louds instead).
func TestDeviceSoulCore_EmptyTypeNoLampFallback(t *testing.T) {
	if _, has := soulFor(t, ""); has {
		t.Error("empty device_type must NOT resolve the lamp soul (no fallback)")
	}
}

// An unknown device type has no profile on disk → no soul, no override.
func TestDeviceSoulCore_UnknownTypeHasNoSoul(t *testing.T) {
	if _, has := soulFor(t, "does-not-exist"); has {
		t.Error("unknown device type must return hasSoul=false (no embedded fallback)")
	}
}

// openclawDefaultSoul is the gateway's own default soul that OpenClaw seeds into
// workspace/SOUL.md on first boot. Onboarding's device block is meant to override
// it — keeping it below `---` is the SOUL.md duplication bug.
const openclawDefaultSoul = `# SOUL.md - Who You Are

_You're not a chatbot. You're becoming someone._

## Core Truths

**Have opinions.** You're allowed to disagree.

## Related

- [SOUL.md personality guide](/concepts/soul)
`

// soulService builds a Service whose OpenclawConfigDir is an isolated temp dir and
// whose device soul resolves from the committed devices/ tree.
func soulService(t *testing.T, deviceType string) (*Service, string) {
	t.Helper()
	t.Setenv("DEVICES_DIR", repoDevicesDir(t))
	t.Setenv("DEVICE_TYPE", deviceType)
	cfgDir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(cfgDir, "workspace"), 0o755); err != nil {
		t.Fatalf("mkdir workspace: %v", err)
	}
	return &Service{config: &config.Config{OpenclawConfigDir: cfgDir, DeviceType: deviceType}}, cfgDir
}

func readSoul(t *testing.T, cfgDir string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(cfgDir, "workspace", "SOUL.md"))
	if err != nil {
		t.Fatalf("read SOUL.md: %v", err)
	}
	return string(b)
}

// When the OpenClaw gateway has seeded its own default soul, ensureSoulMDBlock must
// replace it with the device block — NOT keep it below `---` (the duplication bug).
func TestEnsureSoulMDBlock_StripsOpenClawDefault(t *testing.T) {
	s, cfgDir := soulService(t, "lamp")
	soulPath := filepath.Join(cfgDir, "workspace", "SOUL.md")
	if err := os.WriteFile(soulPath, []byte(openclawDefaultSoul), 0o644); err != nil {
		t.Fatalf("seed default soul: %v", err)
	}

	if _, err := s.ensureSoulMDBlock(); err != nil {
		t.Fatalf("ensureSoulMDBlock: %v", err)
	}

	got := readSoul(t, cfgDir)
	if strings.Contains(got, "# SOUL.md - Who You Are") {
		t.Errorf("openclaw default soul not stripped — duplication bug present:\n%s", got)
	}
	if !strings.Contains(got, "You are **Lamp**") {
		t.Errorf("device (lamp) soul missing after injection:\n%s", got)
	}
	if strings.Count(got, osMandatoryMarker) != 1 {
		t.Errorf("expected exactly one managed block, got %d markers", strings.Count(got, osMandatoryMarker))
	}
}

// Already-dup'd file (device block + openclaw default below `---`) must self-heal:
// the fast path has to fall through and strip the lingering default.
func TestEnsureSoulMDBlock_HealsExistingDuplicate(t *testing.T) {
	s, cfgDir := soulService(t, "lamp")
	// Build the current canonical block, then append the openclaw default below it
	// — exactly the on-device dup shape.
	core, has, err := s.deviceSoulCore()
	if err != nil || !has {
		t.Fatalf("deviceSoulCore: has=%v err=%v", has, err)
	}
	block := osMandatoryMarker + "\n" + strings.TrimSpace(string(core)) + "\n---"
	dup := block + "\n\n" + openclawDefaultSoul
	soulPath := filepath.Join(cfgDir, "workspace", "SOUL.md")
	if err := os.WriteFile(soulPath, []byte(dup), 0o644); err != nil {
		t.Fatalf("seed dup: %v", err)
	}

	changed, err := s.ensureSoulMDBlock()
	if err != nil {
		t.Fatalf("ensureSoulMDBlock: %v", err)
	}
	if !changed {
		t.Fatal("expected ensureSoulMDBlock to heal the duplicate (changed=true)")
	}
	got := readSoul(t, cfgDir)
	if strings.Contains(got, "# SOUL.md - Who You Are") {
		t.Errorf("duplicate not healed:\n%s", got)
	}

	// Idempotent: a second run on the healed file must be a no-op (no churn).
	changed2, err := s.ensureSoulMDBlock()
	if err != nil {
		t.Fatalf("ensureSoulMDBlock (2nd): %v", err)
	}
	if changed2 {
		t.Errorf("second run rewrote a clean file — churn:\n%s", readSoul(t, cfgDir))
	}
}

// An owner `## Personal` section below the default must be preserved while the
// default soul above it is discarded.
func TestEnsureSoulMDBlock_PreservesOwnerPersonal(t *testing.T) {
	s, cfgDir := soulService(t, "lamp")
	const ownerNote = "My owner likes tea at 9pm."
	seed := openclawDefaultSoul + "\n## Personal\n\n" + ownerNote + "\n"
	soulPath := filepath.Join(cfgDir, "workspace", "SOUL.md")
	if err := os.WriteFile(soulPath, []byte(seed), 0o644); err != nil {
		t.Fatalf("seed: %v", err)
	}

	if _, err := s.ensureSoulMDBlock(); err != nil {
		t.Fatalf("ensureSoulMDBlock: %v", err)
	}
	got := readSoul(t, cfgDir)
	if strings.Contains(got, "# SOUL.md - Who You Are") {
		t.Errorf("default soul above ## Personal not stripped:\n%s", got)
	}
	if !strings.Contains(got, ownerNote) {
		t.Errorf("owner ## Personal content lost:\n%s", got)
	}
}

func head(b []byte) string {
	const n = 48
	if len(b) < n {
		return string(b)
	}
	return string(b[:n])
}
