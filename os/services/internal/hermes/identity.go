package hermes

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

// hermesHome is the Hermes data dir (matches internal/hermes/install.sh HERMES_DIR
// and the persona-migration target). SOUL.md — the file Hermes loads as its
// identity prompt — lives at its root.
const hermesHome = "/root/.hermes"

// identitySoulHeading marks the identity block the openclaw→hermes migration
// inlines into SOUL.md (see internal/agent/migrate_persona). UpdateIdentityName
// appends one with this heading when the soul has no name line yet — keep the text
// identical to that migration so the two paths stay consistent.
const identitySoulHeading = "## Your identity card"

// UpdateIdentityName rewrites the agent's name under Hermes by editing the
// `**Name:**` line in <hermes>/SOUL.md — the file Hermes loads as its identity.
// Hermes has no separate IDENTITY.md slot (its own claw-migrate archives that
// file; the openclaw→hermes migration inlines the name into SOUL under an
// identity-card block instead). Replaces the existing name in place, or appends a
// fresh identity-card block when none exists. Atomic tmp+rename so a mid-write
// crash can't truncate the soul. Takes effect on the next Hermes session (no
// gateway restart is forced).
func (s *HermesService) UpdateIdentityName(name string) error {
	name = strings.TrimSpace(name)
	if name == "" {
		return fmt.Errorf("identity name is required")
	}
	soulPath := filepath.Join(hermesHome, "SOUL.md")
	existing, err := os.ReadFile(soulPath)
	if err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("read %s: %w", soulPath, err)
	}

	updated := rewriteSoulName(string(existing), name)

	dir := filepath.Dir(soulPath)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("mkdir %s: %w", dir, err)
	}
	tmp, err := os.CreateTemp(dir, ".SOUL.*.tmp")
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
	if err := os.Rename(tmpPath, soulPath); err != nil {
		os.Remove(tmpPath)
		return fmt.Errorf("rename: %w", err)
	}
	slog.Info("identity name updated", "component", "hermes", "name", name, "path", soulPath)
	return nil
}

// rewriteSoulName returns content with the first `**name:**` line's value replaced
// by name (preserving the bullet prefix, dropping any trailing description). When
// no name line exists, appends a fresh identity-card block so Hermes — which reads
// SOUL.md — picks up the name.
func rewriteSoulName(content, name string) string {
	lines := strings.Split(content, "\n")
	for i, line := range lines {
		idx := strings.Index(strings.ToLower(line), "**name:**")
		if idx < 0 {
			continue
		}
		lines[i] = line[:idx] + "**Name:** " + name
		return strings.Join(lines, "\n")
	}
	prefix := strings.TrimRight(content, "\n")
	if prefix != "" {
		prefix += "\n"
	}
	return prefix + "\n" + identitySoulHeading + "\n\n- **Name:** " + name + "\n"
}

// WatchIdentity polls SOUL.md and pushes updated wake words to HAL + the i18n
// device name whenever the agent's name changes (e.g. the user says "call
// yourself Noah"). Mirrors internal/openclaw/service_identity.go WatchIdentity —
// the only differences are the watched file (SOUL.md, since Hermes has no
// IDENTITY.md slot — the name is inlined as a `**Name:**` card line) and the
// log component. Wake words come from the shared i18n.BuildChitchatWakeWords
// (OpenClaw's private buildWakeWords is the same 5 variants); i18n.SetDeviceName
// then refreshes the {name}/{Name} placeholders + chitchat strip list itself.
func (s *HermesService) WatchIdentity(ctx context.Context) {
	soulPath := filepath.Join(hermesHome, "SOUL.md")
	var lastName string
	for {
		select {
		case <-ctx.Done():
			return
		case <-time.After(5 * time.Second):
		}
		data, err := os.ReadFile(soulPath)
		if err != nil {
			continue
		}
		name := parseSoulName(string(data))
		if name == "" || name == lastName {
			continue
		}
		lastName = name
		words := i18n.BuildChitchatWakeWords(name)
		slog.Info("agent renamed, updating wake words", "component", "hermes", "name", name, "words", words)
		hal.SetVoiceConfig(words)
		i18n.SetDeviceName(name) // {name}/{Name} + chitchat strip follow the agent name too
	}
}

// parseSoulName extracts the agent name from the `- **Name:** <value>` card line
// in SOUL.md. Mirrors openclaw.parseIdentityName: same line format, same trailing-
// description strip (" — ...", " - ...", " | ..."). Only the explicit owner-set
// name line is read — not the soul body's "You are **X**" — matching what
// UpdateIdentityName writes.
func parseSoulName(content string) string {
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
