package migratepersona

import (
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

// quarantineRotateBytes caps the sidecar: past this it is renamed to `.1` and a
// fresh one started, so a runaway agent cannot fill the disk with its own poison.
const quarantineRotateBytes = 64 * 1024

// guardBackupsKept caps the `.bak-<nano>` copies the guard leaves next to a
// file. Every trip writes one, and heartbeat churn (a People-sync rewrite
// every ~30 min) was ~50 files a day in the workspace root; five is enough to
// recover the last few agent writes and nothing older is worth a support
// engineer's time. The retire pass keeps its own backups and is not pruned.
const guardBackupsKept = 5

// GuardAction is one file the guard changed (execute=true) or would change.
type GuardAction struct {
	Path    string
	Dropped []Quarantined
	Written bool
	// WrittenSha8 is Sha8 of the content the guard wrote (Written only). The
	// watcher matches the follow-up fsnotify event against it instead of
	// re-reading the file, which an agent write could have replaced already.
	WrittenSha8 string
}

// MemoryFilePaths returns every runtime's MEMORY.md, deduped and sorted —
// sourced from the adapters so a new runtime cannot be forgotten (mirrors
// UserProfilePaths).
func MemoryFilePaths(opts Options) []string {
	seen := map[string]bool{}
	var out []string
	for _, a := range adapters {
		p := a.memoryFilePath(opts)
		if p == "" || seen[p] {
			continue
		}
		seen[p] = true
		out = append(out, p)
	}
	sort.Strings(out)
	return out
}

// QuarantinePath is the sidecar that receives what the guard removed from path.
// It is `.txt`, never `.md`: PicoClaw's MEMORY.md lives in `<ws>/memory/` and
// HAL's OpenClawContextManager globs `memory/*.md` into the realtime session
// (and distils it into device_summary.md), so a `.md` sidecar would re-inject
// exactly what the guard removed.
func QuarantinePath(path string) string { return path + ".quarantine.txt" }

// GuardMemoryFile runs the rule for path (USER.md → strict allowlist, anything
// else → MEMORY.md prescription rule). Absent or clean files return (nil, nil)
// and are NOT written — USER.md is in the cached prompt prefix, so a rewrite
// costs a cache miss on the next turn. When something is dropped and execute
// is true: `.bak-<nano>` copy, append to the sidecar, then atomic replace.
func GuardMemoryFile(path string, enrolled map[string]bool, execute bool) (*GuardAction, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var out string
	var dropped []Quarantined
	if strings.EqualFold(filepath.Base(path), "USER.md") {
		out, dropped = GuardUserProfileText(string(raw), enrolled)
	} else {
		out, dropped = GuardMemoryText(string(raw))
	}
	if len(dropped) == 0 {
		return nil, nil
	}
	act := &GuardAction{Path: path, Dropped: dropped}
	if !execute {
		return act, nil
	}
	if err := backupFile(path); err != nil {
		return act, fmt.Errorf("backup before quarantine: %w", err)
	}
	if err := pruneBackups(path, guardBackupsKept); err != nil {
		// The backup that matters (this one) is on disk; a leftover old copy
		// is not worth refusing to clean the file over.
		slog.Warn("memory guard: prune old backups failed", "component", "memory-guard", "path", path, "error", err)
	}
	if err := appendQuarantine(path, dropped); err != nil {
		return act, fmt.Errorf("write quarantine: %w", err)
	}
	if err := writeFileAtomic(path, out); err != nil {
		return act, err
	}
	act.Written = true
	act.WrittenSha8 = Sha8([]byte(out))
	return act, nil
}

// pruneBackups removes all but the newest keep `<path>.bak-<n>` files, newest
// meaning the largest numeric suffix (backupFile stamps UnixNano). A file whose
// suffix is not a number is not ours and is left alone.
func pruneBackups(path string, keep int) error {
	matches, err := filepath.Glob(path + ".bak-*")
	if err != nil {
		return fmt.Errorf("glob backups: %w", err)
	}
	type bak struct {
		name string
		n    int64
	}
	var baks []bak
	for _, m := range matches {
		n, err := strconv.ParseInt(strings.TrimPrefix(m, path+".bak-"), 10, 64)
		if err != nil {
			continue
		}
		baks = append(baks, bak{name: m, n: n})
	}
	if len(baks) <= keep {
		return nil
	}
	sort.Slice(baks, func(i, j int) bool { return baks[i].n > baks[j].n })
	for _, b := range baks[keep:] {
		if err := os.Remove(b.name); err != nil && !os.IsNotExist(err) {
			return fmt.Errorf("remove %s: %w", b.name, err)
		}
	}
	return nil
}

// GuardMemoryFiles sweeps every runtime's USER.md and MEMORY.md. Every runtime,
// not just the active one: persona files are copies and an untouched poisoned
// copy migrates back on the next runtime switch (that is exactly why the
// runtime switch did not help on lamp-dbda).
//
// An unreadable enrollment store does NOT stop the sweep (unlike the retire
// pass): the shape and prescription rules need no enrollment, only the label
// check does, and that one is skipped.
func GuardMemoryFiles(opts Options, execute bool) ([]GuardAction, error) {
	enrolled, err := enrolledLabels()
	if err != nil {
		slog.Warn("memory guard: enrollment store unreadable; label check skipped",
			"component", "memory-guard", "error", err)
		enrolled = nil
	}
	var actions []GuardAction
	for _, p := range append(UserProfilePaths(opts), MemoryFilePaths(opts)...) {
		act, err := GuardMemoryFile(p, enrolled, execute)
		if err != nil {
			return actions, fmt.Errorf("guard %s: %w", p, err)
		}
		if act != nil {
			actions = append(actions, *act)
		}
	}
	return actions, nil
}

// appendQuarantine records the dropped blocks, newest last, so the owner (or a
// support engineer over SSH) can see what the agent wrote and why it was
// removed. Rotated once past quarantineRotateBytes to `.quarantine.txt.1`.
func appendQuarantine(path string, dropped []Quarantined) error {
	side := QuarantinePath(path)
	if st, err := os.Stat(side); err == nil && st.Size() > quarantineRotateBytes {
		if err := os.Rename(side, side+".1"); err != nil {
			return fmt.Errorf("rotate: %w", err)
		}
	}
	f, err := os.OpenFile(side, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()
	var sb strings.Builder
	fmt.Fprintf(&sb, "## %s\n", time.Now().Format(time.RFC3339))
	for _, d := range dropped {
		fmt.Fprintf(&sb, "- (%s) %s\n", d.Reason, d.Text)
	}
	sb.WriteString("\n")
	_, err = f.WriteString(sb.String())
	return err
}

// writeFileAtomic writes via temp file + rename so a live agent reading the file
// mid-turn never sees a half-written file. The guard runs while the gateway is
// up (on a watch event, seconds after the agent wrote), so this is mandatory.
func writeFileAtomic(path, content string) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, "."+filepath.Base(path)+".tmp-*")
	if err != nil {
		return fmt.Errorf("create temp: %w", err)
	}
	tmpName := tmp.Name()
	defer func() { _ = os.Remove(tmpName) }() // no-op once renamed

	if _, err := tmp.WriteString(content); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("write temp: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close temp: %w", err)
	}
	if err := os.Chmod(tmpName, 0o644); err != nil {
		return fmt.Errorf("chmod temp: %w", err)
	}
	if err := os.Rename(tmpName, path); err != nil {
		return fmt.Errorf("rename: %w", err)
	}
	return nil
}

// EnrolledLabels exposes enrolledLabels for callers outside the package that
// run a single-file guard. nil (not an error) when the store is unreadable —
// the guard then skips the label check.
func EnrolledLabels() map[string]bool {
	m, err := enrolledLabels()
	if err != nil {
		return nil
	}
	return m
}

// RuntimeOfPath names the runtime whose USER.md or MEMORY.md path is, or "".
func RuntimeOfPath(opts Options, path string) string {
	for r, a := range adapters {
		if a.userProfilePath(opts) == path || a.memoryFilePath(opts) == path {
			return string(r)
		}
	}
	return ""
}
