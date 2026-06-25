// Package migratepersona migrates the persona + long-term memory footprint
// between agent runtimes when the active backend is switched (see
// internal/agent/factory.go).
//
// Design: hub-and-spoke. Each runtime has ONE read adapter (its on-disk layout →
// PersonaBundle) and ONE write adapter (bundle → its layout); a migration is
// read[from] → write[to]. Adding a runtime is a single adapter file that
// interoperates with every existing runtime in both directions — LINEAR (2 files
// per runtime), not the quadratic N×(N-1) a per-pair migrator needs.
//
// Scope is deliberately narrow — persona + long-term memory only:
//
//	SOUL.md      the persona / character file   (whole-file, brand-rewritten)
//	IDENTITY     the owner's name/vibe fields    (native slot per runtime)
//	MEMORY.md    long-term memory entries        (parsed, merged, deduped)
//	USER.md      user profile entries            (same entry-merge as memory)
//	KNOWLEDGE/daily  distilled + per-day memory   (slot-bearing runtimes only)

package migratepersona

import (
	"fmt"
	"path/filepath"
)

// Runtime identifies an agent backend whose persona lives on-device in an
// os-server-managed layout. Only registered runtimes (see adapters) participate in
// migration — openclaw, hermes, and picoclaw today. A backend with no adapter is
// skipped by the reconciler.
type Runtime string

const (
	RuntimeOpenclaw Runtime = "openclaw"
	RuntimeHermes   Runtime = "hermes"
	RuntimePicoclaw Runtime = "picoclaw"
)

// runtimeAdapter is the read/write surface every migratable runtime implements.
type runtimeAdapter interface {
	runtime() Runtime
	read(opts Options) (*PersonaBundle, error)
	write(b *baseMigrator, bundle *PersonaBundle, opts Options) error
}

// adapters is the registry. To make a new runtime migratable, implement
// runtimeAdapter in runtime_<name>.go and add it here — nothing else changes.
var adapters = map[Runtime]runtimeAdapter{
	RuntimeOpenclaw: openclawAdapter{},
	RuntimeHermes:   hermesAdapter{},
	RuntimePicoclaw: picoclawAdapter{},
}

// CanMigrate reports whether a runtime participates in persona migration (has a
// registered adapter). Used by the switch reconciler to skip external runtimes.
func CanMigrate(r Runtime) bool {
	_, ok := adapters[r]
	return ok
}

// Direction names a migration as "<from>_to_<to>" — the Report label and the
// legacy entry point. Kept for the two original pairs; new runtimes do NOT need
// new Direction constants (use RunMigration with Runtime values).
type Direction string

const (
	OpenclawToHermes Direction = "openclaw_to_hermes"
	HermesToOpenclaw Direction = "hermes_to_openclaw"
)

// Per-item outcome statuses.
const (
	StatusMigrated = "migrated"
	StatusSkipped  = "skipped"
	StatusConflict = "conflict"
	StatusError    = "error"
)

// Default char limits for memory files.
const (
	DefaultMemoryCharLimit = 2200
	DefaultUserCharLimit   = 1375
)

// Options controls a migration run. Use DefaultOptions to derive the standard
// on-device paths, then tweak as needed.
type Options struct {
	// OpenclawWorkspace is the OpenClaw workspace dir holding SOUL.md /
	// MEMORY.md / USER.md / KNOWLEDGE.md / memory/ (e.g. /root/.openclaw/workspace).
	OpenclawWorkspace string
	// HermesRoot is the Hermes home dir (e.g. /root/.hermes). SOUL.md lives at
	// its root; MEMORY.md / USER.md live under memories/.
	HermesRoot string
	// PicoclawWorkspace is the PicoClaw workspace dir (e.g.
	// /root/.picoclaw/workspace). Layout matches OpenClaw — SOUL.md / IDENTITY.md /
	// USER.md / KNOWLEDGE.md / memory/ — EXCEPT MEMORY.md lives under memory/.
	PicoclawWorkspace string

	// Execute writes changes; false performs a dry-run (records intended actions,
	// touches nothing).
	Execute bool
	// Overwrite replaces an existing SOUL.md at the destination. Memory files
	// always merge, never overwrite, so this only affects SOUL.md.
	Overwrite bool
	// IncludeDailyMemory folds a slot-bearing runtime's daily memory/*.md into the
	// migrated memory (OpenClaw read only; runtimes without daily files ignore it).
	IncludeDailyMemory bool

	MemoryCharLimit int
	UserCharLimit   int
}

func DefaultOptions(openclawConfigDir, hermesRoot string) Options {
	if openclawConfigDir == "" {
		openclawConfigDir = "/root/.openclaw"
	}
	if hermesRoot == "" {
		hermesRoot = "/root/.hermes"
	}
	return Options{
		OpenclawWorkspace:  filepath.Join(openclawConfigDir, "workspace"),
		HermesRoot:         hermesRoot,
		PicoclawWorkspace:  "/root/.picoclaw/workspace",
		IncludeDailyMemory: true,
		MemoryCharLimit:    DefaultMemoryCharLimit,
		UserCharLimit:      DefaultUserCharLimit,
	}
}

// withDefaults fills any unset limits so a zero-value Options still works.
func (o Options) withDefaults() Options {
	if o.MemoryCharLimit <= 0 {
		o.MemoryCharLimit = DefaultMemoryCharLimit
	}
	if o.UserCharLimit <= 0 {
		o.UserCharLimit = DefaultUserCharLimit
	}
	return o
}

// ItemResult is the outcome of migrating one file (SOUL / IDENTITY / MEMORY / …).
type ItemResult struct {
	Kind        string         `json:"kind"`
	Source      string         `json:"source,omitempty"`
	Destination string         `json:"destination,omitempty"`
	Status      string         `json:"status"`
	Reason      string         `json:"reason,omitempty"`
	Details     map[string]any `json:"details,omitempty"`
}

// Report is the structured result of a migration run.
type Report struct {
	Direction string         `json:"direction"`
	Mode      string         `json:"mode"` // "execute" | "dry-run"
	Items     []ItemResult   `json:"items"`
	Summary   map[string]int `json:"summary"`
}

// RunMigration migrates persona + memory from one runtime to another: read the
// source layout into the canonical bundle, then write it into the destination
// layout. This is the hub — any (from, to) pair of registered runtimes works.
func RunMigration(from, to Runtime, opts Options) (*Report, error) {
	opts = opts.withDefaults()
	src, ok := adapters[from]
	if !ok {
		return nil, fmt.Errorf("migratepersona: no adapter for source runtime %q", from)
	}
	dst, ok := adapters[to]
	if !ok {
		return nil, fmt.Errorf("migratepersona: no adapter for destination runtime %q", to)
	}

	bundle, err := src.read(opts)
	if err != nil {
		return nil, fmt.Errorf("migratepersona: read %s: %w", from, err)
	}

	base := &baseMigrator{opts: opts}
	if err := dst.write(base, bundle, opts); err != nil {
		return nil, fmt.Errorf("migratepersona: write %s: %w", to, err)
	}
	return base.report(Direction(string(from) + "_to_" + string(to))), nil
}

// Run is the legacy Direction-keyed entry point (used by existing callers/tests).
// New code should prefer RunMigration with Runtime values.
func Run(dir Direction, opts Options) (*Report, error) {
	switch dir {
	case OpenclawToHermes:
		return RunMigration(RuntimeOpenclaw, RuntimeHermes, opts)
	case HermesToOpenclaw:
		return RunMigration(RuntimeHermes, RuntimeOpenclaw, opts)
	default:
		return nil, fmt.Errorf("migratepersona: unknown direction %q", dir)
	}
}
