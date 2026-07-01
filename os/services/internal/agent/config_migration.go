package agent

import (
	"log/slog"

	migrateconfig "go.autonomous.ai/os/internal/agent/migrate_config"
	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/server/config"
)

// ConfigMigration carries LLM provider config (API key + base URL) from the previous
// agent runtime to the current one when the backend is switched.
//
// It mirrors ChannelReconcile in marker strategy: it uses LLMConfigAppliedRuntime in
// config.json (not agent_state.json) as its gate, so a failed migration is retried on
// the next boot independently of PersonaMigration's agent_state.json advance.
//
// Startup order in config_watch.go:
//   1. personaMigration.Reconcile()   — persona/memory files
//   2. configMigration.Reconcile()    — LLM config files  ← this
//   3. channelReconcile.Reconcile()   — channels
//   4. mcpReconcile.Reconcile()       — MCP connectors
//   5. agentGateway.EnsureOnboarding() — openclaw.json patches (ensureProviderConfig fallback)
//
// Running before EnsureOnboarding ensures ensureProviderConfig sees already-migrated
// values and is a no-op on a clean switch.
//
// Restart: the target gateway starts before this reconcile runs (switch-runtime
// already launched it). After writing the migrated config to the target's files, we
// call agentGateway.RestartAgent() so the gateway reloads the new values. The marker
// is advanced only after write + config.json sync + restart all succeed — any failure
// leaves the marker un-advanced so the next boot retries the full migration.
type ConfigMigration struct {
	cfg *config.Config
	gw  domain.AgentGateway
	opts migrateconfig.Options
}

// ProvideConfigMigration is the Wire provider.
func ProvideConfigMigration(cfg *config.Config, gw domain.AgentGateway) *ConfigMigration {
	return &ConfigMigration{
		cfg:  cfg,
		gw:   gw,
		opts: migrateconfig.DefaultOptions(cfg.OpenclawConfigDir, hermesHome),
	}
}

// Reconcile runs LLM config migration when the runtime changed since the last
// successful migration. Never blocks startup; failed migrations are retried on the
// next boot because the marker is only advanced on full success.
func (c *ConfigMigration) Reconcile() {
	current := c.cfg.AgentRuntime
	if current == "" {
		current = domain.AgentRuntimeOpenClaw
	}

	// No switch detected — nothing to do.
	if c.cfg.LLMConfigAppliedRuntime == current {
		return
	}

	// First boot with this feature (marker never set): record baseline without
	// migrating, same pattern as ChannelReconcile. Avoids a spurious restart on
	// every device on the upgrade boot.
	if c.cfg.LLMConfigAppliedRuntime == "" {
		if err := c.cfg.WithLockSave(func(cfg *config.Config) {
			cfg.LLMConfigAppliedRuntime = current
		}); err != nil {
			slog.Warn("LLM config migration: record baseline failed", "component", "agent", "error", err)
		} else {
			slog.Info("LLM config migration: baseline recorded (no migrate)", "component", "agent", "runtime", current)
		}
		return
	}

	prev := c.cfg.LLMConfigAppliedRuntime
	from := migrateconfig.Runtime(prev)
	to := migrateconfig.Runtime(current)

	if !migrateconfig.CanMigrate(from) || !migrateconfig.CanMigrate(to) {
		// One of the runtimes has no adapter — record baseline to avoid retry loop.
		if err := c.cfg.WithLockSave(func(cfg *config.Config) {
			cfg.LLMConfigAppliedRuntime = current
		}); err != nil {
			slog.Warn("LLM config migration: record no-adapter baseline failed", "component", "agent", "error", err)
		}
		return
	}

	slog.Info("agent runtime switched — migrating LLM config",
		"component", "agent", "from", prev, "to", current)

	// Step 1: read source runtime's actual on-disk config (may differ from config.json
	// if the agent self-edited its native files via chat).
	migrated, err := migrateconfig.ReadConfig(from, c.opts)
	if err != nil {
		slog.Error("LLM config migration: read source failed; will retry next boot",
			"component", "agent", "from", prev, "to", current, "error", err)
		return
	}
	if migrated.Empty() {
		slog.Info("LLM config migration: source had no config to carry; recording marker",
			"component", "agent", "from", prev, "to", current)
		if err := c.cfg.WithLockSave(func(cfg *config.Config) {
			cfg.LLMConfigAppliedRuntime = current
		}); err != nil {
			slog.Warn("LLM config migration: advance marker failed", "component", "agent", "error", err)
		}
		return
	}

	// Step 2: sync config.json FIRST — before writing to the target runtime's native
	// files. This ensures ensureProviderConfig (the fallback in EnsureOnboarding) always
	// sees consistent values: if step 3 (write to target) fails, ensureProviderConfig
	// reads the already-updated config.json and correctly patches the target instead of
	// overwriting it with stale config.json values (the s.config.LLMAPIKey = Y vs
	// openclaw.json apiKey = X overwrite bug).
	if err := c.cfg.WithLockSave(func(cfg *config.Config) {
		if migrated.APIKey != "" {
			cfg.LLMAPIKey = migrated.APIKey
		}
		if migrated.BaseURL != "" {
			cfg.LLMBaseURL = migrated.BaseURL
		}
		cfg.LLMConfigAppliedRuntime = current
	}); err != nil {
		slog.Warn("LLM config migration: failed to sync config.json; will retry next boot",
			"component", "agent", "error", err)
		return
	}

	// Step 3: write to the target runtime's native config files. If this fails,
	// config.json is already updated (step 2), so ensureProviderConfig will correctly
	// patch the target on the next EnsureOnboarding run — no stale overwrite possible.
	if err := migrateconfig.WriteConfig(to, migrated, c.opts); err != nil {
		slog.Warn("LLM config migration: config.json synced but write to target runtime failed; ensureProviderConfig will patch on next turn",
			"component", "agent", "from", prev, "to", current, "error", err)
		// Marker already advanced — ensureProviderConfig is the fallback from here.
		return
	}

	// Step 4: restart so the target gateway reloads the newly-written config files.
	if err := c.gw.RestartAgent(); err != nil {
		slog.Warn("LLM config migration: config written but gateway restart failed",
			"component", "agent", "from", prev, "to", current, "error", err)
		return
	}

	slog.Info("LLM config migration complete",
		"component", "agent", "from", prev, "to", current)
}
