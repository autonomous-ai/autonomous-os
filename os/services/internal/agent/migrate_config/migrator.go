// Package migrateconfig carries LLM provider config (API key + base URL) from one
// agent runtime to another when the active backend is switched.
//
// Design: hub-and-spoke (mirrors migrate_persona). Each runtime has ONE read adapter
// (its on-disk layout → LLMConfig) and ONE write adapter (LLMConfig → its layout).
// A migration is read[from] → write[to]. Adding a runtime is a single adapter file
// that interoperates with every existing runtime — O(N) adapters, not O(N²) pairs.
//
// Why this is separate from migrate_persona: persona files (SOUL.md, MEMORY.md) and
// LLM provider config (api key, base URL) have different sources of truth and
// different failure modes. A persona migration failure leaves the agent with a stale
// persona; a config migration failure leaves the agent unable to call the LLM. Keeping
// them separate lets each fail, log, and retry independently.
//
// Relation to ensureProviderConfig (onboarding.go): that function is a fallback safety
// net — it patches openclaw.json from config.json when fields are missing. This package
// is the main path — it reads the actual on-disk state of the SOURCE runtime (which may
// have drifted from config.json if the agent self-edited its config) and carries it to
// the destination runtime, then syncs config.json to match.
package migrateconfig

import "fmt"

// LLMConfig is the canonical representation of per-device LLM provider settings
// shared across runtimes. Only the fields that runtimes actually store in their
// native configs are included — model selection is runtime-specific and excluded.
type LLMConfig struct {
	APIKey  string
	BaseURL string
}

// Empty reports whether the config carries no useful data.
func (c LLMConfig) Empty() bool {
	return c.APIKey == "" && c.BaseURL == ""
}

// Runtime identifies an agent backend whose LLM config lives on-device.
type Runtime string

const (
	RuntimeOpenclaw Runtime = "openclaw"
	RuntimeHermes   Runtime = "hermes"
	RuntimePicoclaw Runtime = "picoclaw"
)

// runtimeAdapter is the read/write surface every migratable runtime implements.
type runtimeAdapter interface {
	runtime() Runtime
	read(opts Options) (LLMConfig, error)
	write(cfg LLMConfig, opts Options) error
}

var adapters = map[Runtime]runtimeAdapter{
	RuntimeOpenclaw: openclawAdapter{},
	RuntimeHermes:   hermesAdapter{},
	RuntimePicoclaw: picoclawAdapter{},
}

// CanMigrate reports whether a runtime has a registered config adapter.
func CanMigrate(r Runtime) bool {
	_, ok := adapters[r]
	return ok
}

// Options holds the on-device paths for each runtime's config files.
type Options struct {
	OpenclawConfigDir string // e.g. /root/.openclaw
	HermesRoot        string // e.g. /root/.hermes
	PicoclawConfigDir string // e.g. /root/.picoclaw
}

func DefaultOptions(openclawConfigDir, hermesRoot string) Options {
	if openclawConfigDir == "" {
		openclawConfigDir = "/root/.openclaw"
	}
	if hermesRoot == "" {
		hermesRoot = "/root/.hermes"
	}
	return Options{
		OpenclawConfigDir: openclawConfigDir,
		HermesRoot:        hermesRoot,
		PicoclawConfigDir: "/root/.picoclaw",
	}
}

// RunMigration reads LLM config from the source runtime and writes it to the
// destination runtime. Returns the migrated config so the caller can sync
// config.json. Returns an empty LLMConfig (and no error) when the source has
// nothing to migrate.
func RunMigration(from, to Runtime, opts Options) (LLMConfig, error) {
	src, ok := adapters[from]
	if !ok {
		return LLMConfig{}, fmt.Errorf("migrateconfig: no adapter for source runtime %q", from)
	}
	dst, ok := adapters[to]
	if !ok {
		return LLMConfig{}, fmt.Errorf("migrateconfig: no adapter for destination runtime %q", to)
	}

	cfg, err := src.read(opts)
	if err != nil {
		return LLMConfig{}, fmt.Errorf("migrateconfig: read %s: %w", from, err)
	}
	if cfg.Empty() {
		return LLMConfig{}, nil
	}

	if err := dst.write(cfg, opts); err != nil {
		return LLMConfig{}, fmt.Errorf("migrateconfig: write %s: %w", to, err)
	}
	return cfg, nil
}
