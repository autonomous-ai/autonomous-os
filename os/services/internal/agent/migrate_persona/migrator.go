// Package migratepersona migrates the persona + memory footprint between agent
// runtimes when the active backend is switched (see internal/agent/factory.go).
//
// Scope is deliberately narrow — only persona and long-term memory:
//
//	SOUL.md   the persona / character file        (direct copy, brand-rewritten)
//	MEMORY.md long-term memory entries            (parsed, merged, deduped)
//	USER.md   user profile entries                (same entry-merge as memory)

package migratepersona

import (
	"fmt"
	"path/filepath"
)

// Direction names a supported persona/memory migration.
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
// on-device paths from the configured OpenClaw config dir, then tweak as needed.
type Options struct {
	// OpenclawWorkspace is the OpenClaw workspace dir holding SOUL.md /
	// MEMORY.md / USER.md / memory/ (e.g. /root/.openclaw/workspace).
	OpenclawWorkspace string
	// HermesRoot is the Hermes home dir (e.g. /root/.hermes). SOUL.md lives at
	// its root; MEMORY.md / USER.md live under memories/.
	HermesRoot string

	// Execute writes changes; false performs a dry-run (records intended
	// actions, touches nothing).
	Execute bool
	// Overwrite replaces an existing SOUL.md at the destination. Memory files
	// always merge, never overwrite, so this only affects SOUL.md.
	Overwrite bool
	// IncludeDailyMemory merges OpenClaw workspace/memory/*.md daily files into
	// the destination MEMORY (OpenclawToHermes only; Hermes has no daily files).
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

// ItemResult is the outcome of migrating one file (SOUL / MEMORY / USER).
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

// Migrator handles one direction of a migration.
type Migrator interface {
	Direction() Direction      // Which way data moves.
	Migrate() (*Report, error) // Runs migration; errors per item are in the report.
}

// New constructs the migrator for the requested direction.
func New(dir Direction, opts Options) (Migrator, error) {
	opts = opts.withDefaults()
	base := &baseMigrator{opts: opts}
	switch dir {
	case OpenclawToHermes:
		return &openclawToHermes{baseMigrator: base}, nil
	case HermesToOpenclaw:
		return &hermesToOpenclaw{baseMigrator: base}, nil
	default:
		return nil, fmt.Errorf("migratepersona: unknown direction %q", dir)
	}
}

// Run is a convenience for callers that want the report in one call.
func Run(dir Direction, opts Options) (*Report, error) {
	m, err := New(dir, opts)
	if err != nil {
		return nil, err
	}
	return m.Migrate()
}
