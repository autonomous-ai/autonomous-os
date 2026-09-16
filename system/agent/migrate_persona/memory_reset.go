package migratepersona

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"time"
)

// ResetReport lists what ResetMemoryFiles backed up and cleared.
type ResetReport struct {
	BackupDirs []string `json:"backup_dirs"`
	Cleared    []string `json:"cleared"`
	Skipped    []string `json:"skipped"` // targets that did not exist
}

// userProfileResetForm is the blank USER.md form markdown runtimes get back.
// The `## Users` heading is included so the agent's people sync has its slot.
const userProfileResetForm = `- _Learn about the person you're helping. Update this as you go._
- **Name:**
- **What to call them:**
- **Pronouns:** _(optional)_
- **Timezone:**

## Users
`

// realtimeMemoryFiles are HAL's realtime-layer memory under <workspace>/realtime/
// (see hal/config.py REALTIME_MEMORY_PATH and context_manager/base.py).
var realtimeMemoryFiles = []string{"summary.md", "device_summary.md", "memory.jsonl", "memory_raw.jsonl"}

// ResetMemoryFiles is the no-SSH recovery for a self-poisoned memory (#421).
// For EVERY installed runtime it copies USER.md, MEMORY.md, KNOWLEDGE.md and
// the realtime memory files into <workspace>/.memory-reset-<stamp>/, then
// resets USER.md to the blank form (emptied for Hermes, whose file is
// §-delimited entries, not markdown) and removes the rest — onboarding re-seeds
// KNOWLEDGE.md from its template on its next pass, HAL recreates the realtime
// files on the next turn. Every runtime, because persona is multi-homed: a copy
// left behind migrates back on the next switch.
//
// Files only. Session history (OpenClaw sessions, Hermes state.db) is not
// touched — follow with /new if the poison is also in the conversation.
//
// On error the partial progress so far is returned in the report alongside the
// wrapped error, so the caller can log what was already backed up and cleared.
func ResetMemoryFiles(opts Options) (ResetReport, error) {
	var rep ResetReport
	stamp := time.Now().Format("20060102-150405")

	runtimes := make([]string, 0, len(adapters))
	for r := range adapters {
		runtimes = append(runtimes, string(r))
	}
	sort.Strings(runtimes)

	for _, r := range runtimes {
		a := adapters[Runtime(r)]
		root := a.workspaceRoot(opts)
		if root == "" {
			continue
		}
		if st, err := os.Stat(root); err != nil || !st.IsDir() {
			continue // runtime not installed on this device
		}
		user := a.userProfilePath(opts)
		targets := []string{user, a.memoryFilePath(opts), filepath.Join(filepath.Dir(user), "KNOWLEDGE.md")}
		for _, f := range realtimeMemoryFiles {
			targets = append(targets, filepath.Join(root, "realtime", f))
		}
		bak := filepath.Join(root, ".memory-reset-"+stamp)
		touched := false
		for _, p := range targets {
			data, err := os.ReadFile(p)
			if err != nil {
				if os.IsNotExist(err) {
					rep.Skipped = append(rep.Skipped, p)
					continue
				}
				return rep, fmt.Errorf("read %s: %w", p, err)
			}
			if err := os.MkdirAll(bak, 0o755); err != nil {
				return rep, fmt.Errorf("create backup dir: %w", err)
			}
			if err := os.WriteFile(filepath.Join(bak, filepath.Base(p)), data, 0o644); err != nil {
				return rep, fmt.Errorf("backup %s: %w", p, err)
			}
			switch {
			case p == user && Runtime(r) == RuntimeHermes:
				err = writeFileAtomic(p, "")
			case p == user:
				err = writeFileAtomic(p, userProfileResetForm)
			default:
				err = os.Remove(p)
			}
			if err != nil {
				return rep, fmt.Errorf("clear %s: %w", p, err)
			}
			rep.Cleared = append(rep.Cleared, p)
			touched = true
		}
		if touched {
			rep.BackupDirs = append(rep.BackupDirs, bak)
		}
	}
	return rep, nil
}
