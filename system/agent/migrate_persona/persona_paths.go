package migratepersona

import (
	"path/filepath"
	"sort"
)

// PersonaPaths returns every on-disk persona/long-term-memory path this device
// can hold, across ALL registered runtimes — not just the active one.
//
// Why this exists: factory reset used to clear only the ACTIVE gateway (see
// server/system/factoryreset.go). But persona files are COPIES — a runtime
// switch migrates SOUL/IDENTITY/MEMORY/USER/KNOWLEDGE into the destination and
// leaves the source's copy in place, so a device that has ever switched
// backends holds the same profile in several trees at once. Wiping one and
// rebooting looks clean until the next switch migrates an untouched copy back.
//
// Device-observed 2026-09-03 (lamp-ac82): a retired owner's name survived in
// four byte-identical USER.md files (.picoclaw 07-08, .opencode 08-05,
// .openclaw 08-21, .codex 08-21) and the lamp kept greeting a user who had not
// used it for two months.
//
// Paths are deduped and sorted so the wipe order is deterministic and the log
// is readable. Missing paths are fine — osreset.WipePath ignores them.
func PersonaPaths(opts Options) []string {
	seen := map[string]bool{}
	var out []string
	for _, a := range adapters {
		for _, p := range a.personaPaths(opts) {
			if p == "" || seen[p] {
				continue
			}
			seen[p] = true
			out = append(out, p)
		}
	}
	sort.Strings(out)
	return out
}

// openclawLayoutPersonaPaths lists the persona footprint of the OpenClaw
// workspace layout, which codex / claudecode / opencode reproduce verbatim
// (their presync seeds the workspace as a copy). PicoClaw is deliberately NOT
// on this helper — it keeps MEMORY.md inside memory/, so it builds its own list.
//
// Only persona files are listed, never the workspace dir itself: a workspace
// also holds skills/, configs/ and runtime scaffolding that a persona wipe has
// no business removing. The active runtime's own ResetAgent still does the
// broader teardown for the backend that is actually running.
func openclawLayoutPersonaPaths(ws string) []string {
	if ws == "" {
		return nil
	}
	return []string{
		filepath.Join(ws, "SOUL.md"),
		filepath.Join(ws, "IDENTITY.md"),
		filepath.Join(ws, "MEMORY.md"),
		filepath.Join(ws, "USER.md"),
		filepath.Join(ws, "KNOWLEDGE.md"),
		filepath.Join(ws, "memory"),
	}
}
