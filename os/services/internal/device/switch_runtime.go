package device

import (
	"bytes"
	_ "embed"
	"fmt"
	"os"
)

// switch_runtime.sh is the generic, backend-agnostic agentic-backend switcher.
// It is embedded in os-server and written to switchRuntimeBin on demand, so the
// switcher is versioned and OTA-updated together with the binary — no
// imager/setup.sh change is ever needed to ship or update it, and adding a new
// backend is just a runtimes/<name>/install.sh on the CDN.
//
//go:embed switch_runtime.sh
var switchRuntimeScript []byte

const switchRuntimeBin = "/usr/local/bin/switch-runtime"

// ensureSwitchRuntime writes the embedded switcher to switchRuntimeBin when it
// is missing or stale (content drift after an OTA). Idempotent — a no-op once
// the on-disk copy matches the embedded one.
func ensureSwitchRuntime() error {
	if cur, err := os.ReadFile(switchRuntimeBin); err == nil && bytes.Equal(cur, switchRuntimeScript) {
		return nil
	}
	if err := os.WriteFile(switchRuntimeBin, switchRuntimeScript, 0o755); err != nil {
		return fmt.Errorf("write %s: %w", switchRuntimeBin, err)
	}
	return nil
}
