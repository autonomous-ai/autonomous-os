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
//
//  1. personaMigration.Reconcile()   — persona/memory files
//  2. configMigration.Reconcile()    — LLM config files  ← this
//  3. channelReconcile.Reconcile()   — channels
//  4. mcpReconcile.Reconcile()       — MCP connectors
//  5. agentGateway.EnsureOnboarding() — openclaw.json patches (ensureProviderConfig fallback)
//
// Running before EnsureOnboarding ensures ensureProviderConfig sees already-migrated
// values and is a no-op on a clean switch.
//
// Restart: the target gateway starts before this reconcile runs (switch-runtime
// already launched it). After writing the migrated config to the target's files, we
// call agentGateway.RestartAgent() so the gateway reloads the new values. The marker
// is advanced only after all steps succeed — any failure leaves it un-advanced so the
// next boot retries the full migration.
//
// Grep: component=cfg-migration
type ConfigMigration struct {
	cfg  *config.Config
	gw   domain.AgentGateway
	opts migrateconfig.Options
}

const cfgMigComponent = "cfg-migration"

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

	if c.cfg.LLMConfigAppliedRuntime == current {
		slog.Debug("[cfg-migration] no switch detected, skip", "component", cfgMigComponent, "runtime", current)
		return
	}

	// First boot with this feature (marker never set): record baseline without
	// migrating. Avoids a spurious migration on every device on the upgrade boot.
	if c.cfg.LLMConfigAppliedRuntime == "" {
		if err := c.cfg.WithLockSave(func(cfg *config.Config) {
			cfg.LLMConfigAppliedRuntime = current
		}); err != nil {
			slog.Warn("[cfg-migration] baseline record failed", "component", cfgMigComponent, "runtime", current, "error", err)
		} else {
			slog.Info("[cfg-migration] baseline recorded (first boot, no migrate)", "component", cfgMigComponent, "runtime", current)
		}
		return
	}

	prev := c.cfg.LLMConfigAppliedRuntime
	from := migrateconfig.Runtime(prev)
	to := migrateconfig.Runtime(current)

	if !migrateconfig.CanMigrate(from) || !migrateconfig.CanMigrate(to) {
		slog.Info("[cfg-migration] no adapter for runtime pair, skip", "component", cfgMigComponent, "from", prev, "to", current)
		if err := c.cfg.WithLockSave(func(cfg *config.Config) {
			cfg.LLMConfigAppliedRuntime = current
		}); err != nil {
			slog.Warn("[cfg-migration] advance marker failed", "component", cfgMigComponent, "error", err)
		}
		return
	}

	slog.Info("[cfg-migration] switch detected, starting migration", "component", cfgMigComponent, "from", prev, "to", current)

	// Step 1: read source runtime's actual on-disk config.
	migrated, err := migrateconfig.ReadConfig(from, c.opts)
	if err != nil {
		slog.Error("[cfg-migration] step1 read source failed, will retry next boot", "component", cfgMigComponent, "from", prev, "to", current, "error", err)
		return
	}
	if migrated.Empty() {
		slog.Info("[cfg-migration] step1 source empty, nothing to carry", "component", cfgMigComponent, "from", prev, "to", current)
		if err := c.cfg.WithLockSave(func(cfg *config.Config) {
			cfg.LLMConfigAppliedRuntime = current
		}); err != nil {
			slog.Warn("[cfg-migration] advance marker failed", "component", cfgMigComponent, "error", err)
		}
		return
	}
	slog.Info("[cfg-migration] step1 read OK", "component", cfgMigComponent, "from", prev, "has_key", migrated.APIKey != "", "has_url", migrated.BaseURL != "")

	// Step 2: sync LLMAPIKey + LLMBaseURL to config.json — NOT the marker yet.
	if err := c.cfg.WithLockSave(func(cfg *config.Config) {
		if migrated.APIKey != "" {
			cfg.LLMAPIKey = migrated.APIKey
		}
		if migrated.BaseURL != "" {
			cfg.LLMBaseURL = migrated.BaseURL
		}
	}); err != nil {
		slog.Warn("[cfg-migration] step2 config.json sync failed, will retry next boot", "component", cfgMigComponent, "error", err)
		return
	}
	slog.Info("[cfg-migration] step2 config.json synced", "component", cfgMigComponent)

	// Step 3: write to the target runtime's native config files.
	if err := migrateconfig.WriteConfig(to, migrated, c.opts); err != nil {
		slog.Warn("[cfg-migration] step3 write target failed, will retry next boot", "component", cfgMigComponent, "from", prev, "to", current, "error", err)
		return
	}
	slog.Info("[cfg-migration] step3 write target OK", "component", cfgMigComponent, "to", current)

	// Step 4: restart target gateway to reload newly-written config.
	if err := c.gw.RestartAgent(); err != nil {
		slog.Warn("[cfg-migration] step4 restart failed, will retry next boot", "component", cfgMigComponent, "from", prev, "to", current, "error", err)
		return
	}
	slog.Info("[cfg-migration] step4 gateway restarted", "component", cfgMigComponent, "to", current)

	// Step 5: advance marker ONLY after all steps succeed.
	if err := c.cfg.WithLockSave(func(cfg *config.Config) {
		cfg.LLMConfigAppliedRuntime = current
	}); err != nil {
		slog.Warn("[cfg-migration] step5 marker advance failed, will re-run next boot (idempotent)", "component", cfgMigComponent, "error", err)
		return
	}
	slog.Info("[cfg-migration] done", "component", cfgMigComponent, "from", prev, "to", current)
}
