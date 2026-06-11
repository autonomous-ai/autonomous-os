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
