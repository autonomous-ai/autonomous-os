package device

import (
	"os"
	"path/filepath"
	"testing"
)

func writeDeviceMD(t *testing.T, deviceType, body string) {
	t.Helper()
	root := t.TempDir()
	t.Setenv("DEVICES_DIR", root)
	dir := filepath.Join(root, deviceType)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "DEVICE.md"), []byte(body), 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}
}

func TestCapabilities_Present(t *testing.T) {
	writeDeviceMD(t, "lamp", `---
schema: autonomous.device.v1
capabilities:
  audio:   { routes: [audio, speaker, voice], required: true }
  motion:  { routes: [servo], driver: feetech, required: true }
  light:   { routes: [led], driver: ws2812, required: true }
  display: { routes: [display], driver: gc9a01, required: true }
soul_ref: SOUL.md
---

# Lamp
`)
	caps := Capabilities("lamp")
	for _, want := range []string{"audio", "motion", "light", "display"} {
		if !caps[want] {
			t.Fatalf("Capabilities missing %q (got %v)", want, caps)
		}
	}
	if caps["vision"] {
		t.Fatalf("Capabilities should not contain undeclared %q", "vision")
	}
}

func TestCapabilities_NoBlock(t *testing.T) {
	writeDeviceMD(t, "intern", `---
schema: autonomous.device.v1
gateway:
  default: openclaw
---

# Intern
`)
	if caps := Capabilities("intern"); len(caps) != 0 {
		t.Fatalf("Capabilities = %v, want empty", caps)
	}
}

func TestCapabilities_Missing(t *testing.T) {
	t.Setenv("DEVICES_DIR", t.TempDir())
	if caps := Capabilities("ghost"); caps != nil {
		t.Fatalf("Capabilities = %v, want nil", caps)
	}
}

func TestRouteCapability(t *testing.T) {
	cases := map[string]string{
		"/emotion":          CapExpression,
		"/scene":            CapLight,
		"/scene/off":        CapLight,
		"/led/solid":        CapLight,
		"/servo/track":      CapMotion,
		"/servo/track/stop": CapMotion,
		"/display":          CapDisplay,
		"/music/play":       CapMedia,
		"/voice/status":     "", // audio paths fail open (almost always present)
		"/audio/volume":     "",
		"/wellbeing/log":    "", // os-server routes — no hardware gate
		"/buddy/screenshot": "",
		"/speak":            "",
	}
	for path, want := range cases {
		if got := RouteCapability(path); got != want {
			t.Errorf("RouteCapability(%q) = %q, want %q", path, got, want)
		}
	}
}

func TestGatewayDefault_Present(t *testing.T) {
	writeDeviceMD(t, "lamp", `---
schema: autonomous.device.v1
gateway:
  default: hermes
  protocol: websocket
capabilities:
  audio: { required: true }
---

# Lamp
`)
	if got := GatewayDefault("lamp"); got != "hermes" {
		t.Fatalf("GatewayDefault = %q, want %q", got, "hermes")
	}
}

func TestGatewayDefault_NoGatewayBlock(t *testing.T) {
	writeDeviceMD(t, "lamp", `---
schema: autonomous.device.v1
capabilities:
  audio: { required: true }
---

# Lamp
`)
	if got := GatewayDefault("lamp"); got != "" {
		t.Fatalf("GatewayDefault = %q, want empty", got)
	}
}

func TestGatewayDefault_Missing(t *testing.T) {
	t.Setenv("DEVICES_DIR", t.TempDir())
	if got := GatewayDefault("lamp"); got != "" {
		t.Fatalf("GatewayDefault = %q, want empty", got)
	}
}

func TestGatewayProtocol_Present(t *testing.T) {
	writeDeviceMD(t, "lamp", `---
schema: autonomous.device.v1
gateway:
  default: openclaw
  protocol: websocket
capabilities:
  audio: { required: true }
---

# Lamp
`)
	if got := GatewayProtocol("lamp"); got != "websocket" {
		t.Fatalf("GatewayProtocol = %q, want %q", got, "websocket")
	}
}

func TestGatewayProtocol_NoGatewayBlock(t *testing.T) {
	writeDeviceMD(t, "lamp", `---
schema: autonomous.device.v1
capabilities:
  audio: { required: true }
---

# Lamp
`)
	if got := GatewayProtocol("lamp"); got != "" {
		t.Fatalf("GatewayProtocol = %q, want empty", got)
	}
}

func TestStartupVolume_Declared(t *testing.T) {
	writeDeviceMD(t, "lamp", `---
schema: autonomous.device.v1
memory: { backend: local }
startup_volume: 60
---

# Lamp
`)
	if v := StartupVolume("lamp"); v != 60 {
		t.Fatalf("StartupVolume = %d, want 60", v)
	}
}

func TestStartupVolume_DefaultsWhenAbsentOrInvalid(t *testing.T) {
	// Absent → default (100).
	writeDeviceMD(t, "intern", `---
schema: autonomous.device.v1
memory: { backend: local }
---

# Intern
`)
	if v := StartupVolume("intern"); v != DefaultStartupVolume {
		t.Fatalf("absent: StartupVolume = %d, want %d", v, DefaultStartupVolume)
	}
	// Out of range → default (clamp guards a typo from muting/over-driving).
	writeDeviceMD(t, "bad", `---
schema: autonomous.device.v1
startup_volume: 250
---

# Bad
`)
	if v := StartupVolume("bad"); v != DefaultStartupVolume {
		t.Fatalf("out-of-range: StartupVolume = %d, want %d", v, DefaultStartupVolume)
	}
	// Unreadable device dir → default.
	if v := StartupVolume("does-not-exist"); v != DefaultStartupVolume {
		t.Fatalf("missing: StartupVolume = %d, want %d", v, DefaultStartupVolume)
	}
}
