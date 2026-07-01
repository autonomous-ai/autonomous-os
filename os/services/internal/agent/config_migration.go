package agent

import (
	"log/slog"

	migrateconfig "go.autonomous.ai/os/internal/agent/migrate_config"
	"go.autonomous.ai/os/server/config"
)

// ConfigMigration carries LLM provider config (API key + base URL) from the previous
// agent runtime to the current one when the backend is switched.
//
// It mirrors PersonaMigration in structure and startup placement: it runs once in the
// startup sequence, uses the same agent_state.json history to detect switches, and is
// non-blocking. Unlike PersonaMigration it also syncs config.json after the migration
// so the OS source-of-truth stays current with what was on-disk in the source runtime.
//
// Relation to ensureProviderConfig (openclaw/onboarding.go): that function is a
// fallback safety net that patches openclaw.json from config.json when fields are
// missing. This is the main path that reads the SOURCE runtime's actual on-disk state
// (which may have drifted from config.json) and carries it to the destination runtime.
type ConfigMigration struct {
	Prev    string
	Current string
	Needed  bool

	cfg  *config.Config
	opts migrateconfig.Options
}

// ProvideConfigMigration reads agent_state.json to determine whether the runtime
// switched since the last boot, and prepares the migration plan.
func ProvideConfigMigration(cfg *config.Config) *ConfigMigration {
	current, _, _ := resolveRuntime(cfg)
	opts := migrateconfig.DefaultOptions(cfg.OpenclawConfigDir, hermesHome)

	cm := &ConfigMigration{
		Current: current,
		cfg:     cfg,
		opts:    opts,
	}

	st, err := loadAgentState(agentStatePath)
	if err != nil {
		slog.Warn("config migration: read agent state failed; skipping this boot",
			"component", "agent", "error", err)
		return cm
	}

	prev := st.previousRuntime()
	if prev == "" || prev == current {
		return cm
	}

	from := migrateconfig.Runtime(prev)
	to := migrateconfig.Runtime(current)
	if migrateconfig.CanMigrate(from) && migrateconfig.CanMigrate(to) {
		cm.Prev = prev
		cm.Needed = true
	}
	return cm
}

// Reconcile runs LLM config migration after a runtime switch, then syncs config.json.
// Non-blocking and best-effort: errors are logged but never prevent startup.
// ensureProviderConfig (openclaw/onboarding.go) acts as the fallback if this fails.
func (c *ConfigMigration) Reconcile() {
	if !c.Needed {
		return
	}

	from := migrateconfig.Runtime(c.Prev)
	to := migrateconfig.Runtime(c.Current)

	slog.Info("agent runtime switched — migrating LLM config",
		"component", "agent", "from", c.Prev, "to", c.Current)

	migrated, err := migrateconfig.RunMigration(from, to, c.opts)
	if err != nil {
		slog.Error("LLM config migration failed; ensureProviderConfig will use config.json as fallback",
			"component", "agent", "from", c.Prev, "to", c.Current, "error", err)
		return
	}
	if migrated.Empty() {
		slog.Info("LLM config migration: source had no config to carry",
			"component", "agent", "from", c.Prev, "to", c.Current)
		return
	}

	// Sync migrated values back to config.json so it stays the OS source-of-truth.
	if err := c.cfg.WithLockSave(func(cfg *config.Config) {
		if migrated.APIKey != "" {
			cfg.LLMAPIKey = migrated.APIKey
		}
		if migrated.BaseURL != "" {
			cfg.LLMBaseURL = migrated.BaseURL
		}
	}); err != nil {
		slog.Warn("LLM config migration: synced to runtime but failed to update config.json",
			"component", "agent", "error", err)
		return
	}

	slog.Info("LLM config migration complete",
		"component", "agent", "from", c.Prev, "to", c.Current)
}
