package skills

import (
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
)

// StoreDir is the ONE on-disk skill store. Every agentic runtime's native skills
// dir is a symlink to it (LinkRuntimeDir), so a skill is downloaded once, has one
// version, and a runtime switch syncs nothing.
//
// Before this, each runtime kept its own copy of the same CDN zips
// (openclaw workspace/skills, hermes skills/openclaw-imports, picoclaw + codex
// workspace/skills, claudecode ~/.claude/skills). Two failures followed:
//
//   - Drift: two devices on the same os-server release ran different
//     connectors/SKILL.md, because their copies were downloaded at different times.
//   - Duplicates: two candidates for one skill name make Hermes refuse to load the
//     skill at all ("Ambiguous skill name") — see hermes.pruneImportedSkillDuplicates,
//     where a device with a valid Gmail token ended up trying to install a CLI email
//     client because the connectors skill never loaded.
//
// This mirrors what connector CREDENTIALS already do: one canonical path that every
// runtime reads regardless of which one is active.
const StoreDir = "/root/.autonomous/skills"

// storeDir is the mutable seam behind StoreDir so tests can redirect the store away
// from /root. Production code reads it via Dir().
var storeDir = StoreDir

// Dir returns the active store path.
func Dir() string { return storeDir }

// EnsureStore creates the store dir. Safe to call on every boot.
func EnsureStore() error {
	if err := os.MkdirAll(storeDir, 0o755); err != nil {
		return fmt.Errorf("mkdir skill store: %w", err)
	}
	return nil
}

// LinkRuntimeDir makes runtimeDir resolve to the store.
//
// When runtimeDir is a real directory (a device installed by an older os-server),
// its skills are MOVED into the store first — but a skill already in the store WINS:
// the store copy is the one the watcher keeps fresh, so the runtime's is the
// potentially stale duplicate. The real dir is then replaced by the symlink.
//
// Returns true when anything changed on disk, so callers can gate a gateway restart
// on it.
func LinkRuntimeDir(runtimeDir string) (bool, error) {
	if err := EnsureStore(); err != nil {
		return false, err
	}

	if target, err := os.Readlink(runtimeDir); err == nil {
		if target == storeDir {
			return false, nil // already linked correctly
		}
		// A symlink pointing somewhere else — re-point it.
		if err := os.Remove(runtimeDir); err != nil {
			return false, fmt.Errorf("remove stale skills symlink %s: %w", runtimeDir, err)
		}
	}

	// A real dir from an older build: migrate its contents, store copy wins.
	if info, err := os.Lstat(runtimeDir); err == nil && info.IsDir() {
		entries, err := os.ReadDir(runtimeDir)
		if err != nil {
			return false, fmt.Errorf("read %s: %w", runtimeDir, err)
		}
		for _, e := range entries {
			if !e.IsDir() {
				continue
			}
			dst := filepath.Join(storeDir, e.Name())
			if _, err := os.Stat(dst); err == nil {
				continue // store already has it — the runtime copy is the stale one
			}
			if err := os.Rename(filepath.Join(runtimeDir, e.Name()), dst); err != nil {
				slog.Warn("skill store: migrate failed", "component", "skills", "skill", e.Name(), "error", err)
			}
		}
		if err := os.RemoveAll(runtimeDir); err != nil {
			return false, fmt.Errorf("remove legacy skills dir %s: %w", runtimeDir, err)
		}
	}

	if err := os.MkdirAll(filepath.Dir(runtimeDir), 0o755); err != nil {
		return false, fmt.Errorf("mkdir parent of %s: %w", runtimeDir, err)
	}
	if err := os.Symlink(storeDir, runtimeDir); err != nil {
		return false, fmt.Errorf("symlink %s -> %s: %w", runtimeDir, storeDir, err)
	}
	slog.Info("skill store: runtime dir linked", "component", "skills",
		"runtime_dir", runtimeDir, "store", storeDir)
	return true, nil
}

// InstallByName downloads the named skill zips from baseURL into the store and
// returns the names whose content actually changed (content-hash gated), so the
// caller restarts the gateway / notifies the agent only when something moved.
func InstallByName(baseURL string, names []string) []string {
	if baseURL == "" {
		slog.Info("skill install skipped: no skills base URL", "component", "skills")
		return nil
	}
	if err := EnsureStore(); err != nil {
		slog.Warn("skill install: store unavailable", "component", "skills", "error", err)
		return nil
	}

	var changed []string
	for _, name := range names {
		url := fmt.Sprintf("%s/%s.zip", baseURL, name)
		tmpZip, err := DownloadToTempFile(url, "skill-*.zip")
		if err != nil {
			slog.Warn("skill zip download failed", "component", "skills", "skill", name, "error", err)
			continue
		}

		target := filepath.Join(storeDir, name)
		oldHash, _ := FolderHash(target)

		if err := ExtractSkillZip(tmpZip, target); err != nil {
			slog.Warn("skill extract failed", "component", "skills", "skill", name, "error", err)
			os.Remove(tmpZip)
			continue
		}
		os.Remove(tmpZip)

		newHash, _ := FolderHash(target)
		if newHash != oldHash {
			changed = append(changed, name)
		}
	}
	return changed
}

// PruneUnsupported removes store entries whose name is not in keep (the device's
// capability-gated set) and returns what it removed. Fail-open by contract: callers
// pass the full catalog when the device declares no capabilities, so nothing is
// pruned.
func PruneUnsupported(keep map[string]bool) []string {
	entries, err := os.ReadDir(storeDir)
	if err != nil {
		return nil
	}
	var removed []string
	for _, e := range entries {
		if !e.IsDir() || keep[e.Name()] {
			continue
		}
		if err := os.RemoveAll(filepath.Join(storeDir, e.Name())); err != nil {
			slog.Warn("prune unsupported skill failed", "component", "skills", "skill", e.Name(), "error", err)
			continue
		}
		removed = append(removed, e.Name())
		slog.Info("pruned unsupported skill (capability gate)", "component", "skills", "skill", e.Name())
	}
	return removed
}

// SetDirForTest redirects the store for the duration of a test and returns the new
// path. Test-only: production code never calls it.
func SetDirForTest(t interface{ Cleanup(func()) }, dir string) string {
	old := storeDir
	storeDir = dir
	t.Cleanup(func() { storeDir = old })
	return dir
}
