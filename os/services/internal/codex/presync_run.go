package codex

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"strings"
	"time"
)

// Files presync.sh owns — hashed around the run so EnsureOnboarding restarts
// the gateway only when the config actually changed (hermes pattern).
const (
	codexConfigTOML = codexHome + "/config.toml"
	codexEnvFile    = codexHome + "/.env"
)

// runPresync executes the EMBEDDED presync hook (PresyncScript — the same
// script switch-runtime runs right before codex starts). Running it from
// EnsureOnboarding on every boot gives Codex the same self-heal property
// hermes has: a device that boots straight into codex, or an llm_* change
// while codex is active, gets config.toml/.env re-synced from config.json
// without waiting for the next runtime switch.
func (s *CodexService) runPresync() error {
	f, err := os.CreateTemp("", "codex-presync-*.sh")
	if err != nil {
		return fmt.Errorf("create temp: %w", err)
	}
	path := f.Name()
	defer os.Remove(path)
	if _, err := f.Write(PresyncScript); err != nil {
		f.Close()
		return fmt.Errorf("write script: %w", err)
	}
	f.Close()
	if err := os.Chmod(path, 0o755); err != nil {
		return fmt.Errorf("chmod: %w", err)
	}

	// Generous timeout: a normal boot is fast (jq edits only), but a post-reset
	// boot copies the openclaw workspace (presync.sh §1 migrate).
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()
	out, err := exec.CommandContext(ctx, "bash", path).CombinedOutput()
	if len(out) > 0 {
		slog.Info("codex presync output", "component", "codex", "output", strings.TrimSpace(string(out)))
	}
	if err != nil {
		return fmt.Errorf("run presync: %w", err)
	}
	return nil
}

// fileHash returns a content hash of path, or "" when the file is absent — so
// a config.toml that did not exist before and was created by presync reads as
// changed (triggering the restart).
func fileHash(path string) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}
