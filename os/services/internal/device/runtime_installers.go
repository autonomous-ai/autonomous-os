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

// presyncHookPath is where switch_runtime.sh looks for a backend's optional
// pre-start hook (HOOK="/usr/local/bin/runtime-${NEW}-presync"). Keep in sync.
func presyncHookPath(runtime string) string {
	return filepath.Join("/usr/local/bin", "runtime-"+runtime+"-presync")
}

// materializePresync writes the target runtime's embedded pre-start hook (registered
// via runtimereg) to /usr/local/bin/runtime-<runtime>-presync. Idempotent — a no-op
// once the on-disk copy matches. Runtimes that ship no embedded presync return nil
// (the hook is then whatever their installer wrote, if any).
//
// Doing this from os-server — rather than letting install.sh write the hook — is what
// makes a plain os-server OTA refresh the hook on disk: install.sh only re-runs on a
// first install or a failed verify, so a config fix shipped only inside it would never
// reach an already-installed backend. The hermes hook owns config.yaml's model wiring,
// so refreshing it here lets that self-heal on the very next switch.
func materializePresync(runtime string) error {
	script, ok := runtimereg.GetPresync(runtime)
	if !ok {
		return nil
	}
	p := presyncHookPath(runtime)
	if cur, err := os.ReadFile(p); err == nil && bytes.Equal(cur, script) {
		return nil
	}
	if err := os.WriteFile(p, script, 0o755); err != nil {
		return fmt.Errorf("write %s: %w", p, err)
	}
	return nil
}
