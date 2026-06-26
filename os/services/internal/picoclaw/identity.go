package picoclaw

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"time"

	"go.autonomous.ai/os/lib/hal"
	"go.autonomous.ai/os/lib/i18n"
)

// PicoClaw's IDENTITY.md is copied 1-for-1 from OpenClaw (presync.sh §0 carries it
// over on migrate; the format is identical — a `- **Name:** <value>` card line), so
// the identity handling here mirrors internal/openclaw/service_identity.go exactly.
// Only the watched path (PicoClaw's workspace) and the log component differ. Wake
// words come from the shared i18n.BuildChitchatWakeWords (same 5 variants OpenClaw's
// private buildWakeWords produced). PicoClaw runs as root (HOME=/root), so unlike
// OpenClaw there is no runtime-user chown step.

// identityPath returns the workspace IDENTITY.md location.
func identityPath() string {
	return filepath.Join(picoclawWorkspaceDir, "IDENTITY.md")
}

// WatchIdentity polls IDENTITY.md in the PicoClaw workspace and pushes updated wake
// words to HAL whenever the agent's name changes (e.g. the user says "call yourself
// Noah"). Mirrors openclaw.WatchIdentity.
func (s *PicoclawService) WatchIdentity(ctx context.Context) {
	path := identityPath()
	var lastName string
	for {
		select {
		case <-ctx.Done():
			return
		case <-time.After(5 * time.Second):
		}
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		name := parseIdentityName(string(data))
		if name == "" || name == lastName {
			continue
		}
		lastName = name
		words := i18n.BuildChitchatWakeWords(name)
		slog.Info("agent renamed, updating wake words", "component", "picoclaw", "name", name, "words", words)
		hal.SetVoiceConfig(words)
		i18n.SetDeviceName(name) // {name}/{Name} + chitchat strip follow the agent name too
	}
}

// UpdateIdentityName rewrites the `**Name:**` line in workspace/IDENTITY.md so
// downstream consumers (parseIdentityName / WatchIdentity → wake words) see the new
// agent name. Preserves any bullet prefix and everything else; appends a fresh
// `- **Name:** <name>` line when none exists yet. Atomic tmp+rename so a mid-write
// crash cannot truncate the file. Mirrors openclaw.UpdateIdentityName.
func (s *PicoclawService) UpdateIdentityName(name string) error {
	name = strings.TrimSpace(name)
	if name == "" {
		return fmt.Errorf("identity name is required")
	}

	path := identityPath()
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("mkdir %s: %w", dir, err)
	}

	existing, err := os.ReadFile(path)
	if err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("read %s: %w", path, err)
	}

	updated := rewriteIdentityName(string(existing), name)

	tmp, err := os.CreateTemp(dir, ".IDENTITY.*.tmp")
	if err != nil {
		return fmt.Errorf("create tmp: %w", err)
	}
	tmpPath := tmp.Name()
	if _, err := tmp.WriteString(updated); err != nil {
		tmp.Close()
		os.Remove(tmpPath)
		return fmt.Errorf("write tmp: %w", err)
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpPath)
		return fmt.Errorf("close tmp: %w", err)
	}
	if err := os.Rename(tmpPath, path); err != nil {
		os.Remove(tmpPath)
		return fmt.Errorf("rename: %w", err)
	}
	slog.Info("identity name updated", "component", "picoclaw", "name", name, "path", path)
	return nil
}

// rewriteIdentityName returns content with the first `**name:**` line's value
// replaced by name. The line's leading prefix (e.g. "- ") is preserved; any trailing
// description after the value is dropped. When no name line is found,
// "- **Name:** <name>" is appended. Mirrors openclaw.rewriteIdentityName.
func rewriteIdentityName(content, name string) string {
	lines := strings.Split(content, "\n")
	for i, line := range lines {
		idx := strings.Index(strings.ToLower(line), "**name:**")
		if idx < 0 {
			continue
		}
		lines[i] = line[:idx] + "**Name:** " + name
		// Drop a following italic-parens placeholder hint like
		// `  _(pick something you like)_` left over from the onboarding template —
		// once the user picks a name, the hint is stale.
		if i+1 < len(lines) && isItalicPlaceholder(lines[i+1]) {
			lines = append(lines[:i+1], lines[i+2:]...)
		}
		return strings.Join(lines, "\n")
	}
	prefix := content
	if prefix != "" && !strings.HasSuffix(prefix, "\n") {
		prefix += "\n"
	}
	return prefix + "- **Name:** " + name + "\n"
}

// isItalicPlaceholder returns true when line is a markdown italic note wrapped in
// `_(...)_` or `*(...)*` (with optional leading whitespace). Used to detect and
// remove the onboarding-template hint line after rename.
func isItalicPlaceholder(line string) bool {
	t := strings.TrimSpace(line)
	if len(t) < 4 {
		return false
	}
	return (strings.HasPrefix(t, "_(") && strings.HasSuffix(t, ")_")) ||
		(strings.HasPrefix(t, "*(") && strings.HasSuffix(t, ")*"))
}

// parseIdentityName extracts the agent name from the `- **Name:** <value>` line in
// IDENTITY.md. Mirrors openclaw.parseIdentityName (same trailing-description strip).
func parseIdentityName(content string) string {
	for _, line := range strings.Split(content, "\n") {
		line = strings.TrimSpace(line)
		lower := strings.ToLower(line)
		idx := strings.Index(lower, "**name:**")
		if idx < 0 {
			continue
		}
		name := strings.TrimSpace(line[idx+len("**name:**"):])
		if i := strings.IndexAny(name, "—-|"); i > 0 {
			name = strings.TrimSpace(name[:i])
		}
		if name != "" {
			return name
		}
	}
	return ""
}
