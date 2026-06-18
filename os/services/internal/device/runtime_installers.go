package device

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"

	"go.autonomous.ai/os/lib/runtimereg"
)

// embeddedInstallerDir is where os-server materializes binary-embedded backend
// installers. switch_runtime.sh looks here FIRST (before the CDN fallback), so
// any backend compiled into os-server installs fully offline. Keep this path in
// sync with switch_runtime.sh.
const embeddedInstallerDir = "/usr/local/lib/os-runtimes"

// materializeInstaller writes the target runtime's embedded installer (registered
// by its backend package via runtimereg) to embeddedInstallerDir/<runtime>/install.sh.
// Idempotent — a no-op once the on-disk copy matches. Runtimes with no embedded
// installer return nil; switch_runtime.sh then fetches them from the CDN.
func materializeInstaller(runtime string) error {
	script, ok := runtimereg.Get(runtime)
	if !ok {
		return nil
	}
	dir := filepath.Join(embeddedInstallerDir, runtime)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("mkdir %s: %w", dir, err)
	}
	p := filepath.Join(dir, "install.sh")
	if cur, err := os.ReadFile(p); err == nil && bytes.Equal(cur, script) {
		return nil
	}
	if err := os.WriteFile(p, script, 0o755); err != nil {
		return fmt.Errorf("write %s: %w", p, err)
	}
	return nil
}
