package agent

import (
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"time"

	migratepersona "go.autonomous.ai/os/internal/agent/migrate_persona"
	"go.autonomous.ai/os/server/config"
)

const hermesHome = "/root/.hermes"
const agentStatePath = "/root/config/agent_state.json"
const agentStateMaxHistory = 50

type agentRuntimeEntry struct {
	Runtime string `json:"runtime"`
	At      int64  `json:"at"`
}

type agentState struct {
	History []agentRuntimeEntry `json:"history"`
}

func loadAgentState(path string) (*agentState, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return &agentState{}, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read agent state: %w", err)
	}
	var st agentState
	if err := json.Unmarshal(data, &st); err != nil {
		return nil, fmt.Errorf("parse agent state: %w", err)
	}
	return &st, nil
}

func (s *agentState) previousRuntime() string {
	if len(s.History) == 0 {
		return ""
	}
	return s.History[len(s.History)-1].Runtime
}

func appendAgentRuntime(path, runtime string) error {
	st, err := loadAgentState(path)
	if err != nil {
		return err
	}
	if st.previousRuntime() == runtime {
		return nil // already the latest — nothing to record
	}
	st.History = append(st.History, agentRuntimeEntry{Runtime: runtime, At: time.Now().Unix()})
	if len(st.History) > agentStateMaxHistory {
		st.History = st.History[len(st.History)-agentStateMaxHistory:]
	}
	data, err := json.MarshalIndent(st, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal agent state: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("create state dir: %w", err)
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return fmt.Errorf("write agent state: %w", err)
	}
	if err := os.Rename(tmp, path); err != nil {
		return fmt.Errorf("commit agent state: %w", err)
	}
	return nil
}

// Persona migration coordination.
func migrationDirection(prev, current string) (migratepersona.Direction, bool) {
	switch {
	case prev == "openclaw" && current == "hermes":
		return migratepersona.OpenclawToHermes, true
	case prev == "hermes" && current == "openclaw":
		return migratepersona.HermesToOpenclaw, true
	default:
		return "", false
	}
}

// PersonaMigration tracks agent runtime changes and handles persona + memory migration.
type PersonaMigration struct {
	Prev      string                   // last recorded runtime ("" on first boot)
	Current   string                   // runtime the gateway is starting now
	Direction migratepersona.Direction // valid only when Needed
	Needed    bool                     // true when a supported switch was detected

	firstBoot bool
	statePath string
	opts      migratepersona.Options
}

// ProvidePersonaMigration determines the current and previous runtimes, and prepares the migration plan (read-only; marker is updated after migration).
func ProvidePersonaMigration(cfg *config.Config) *PersonaMigration {
	current, _, _ := resolveRuntime(cfg)

	opts := migratepersona.DefaultOptions(cfg.OpenclawConfigDir, hermesHome)
	opts.Execute = true // a runtime switch is an explicit user action — apply it

	pm := &PersonaMigration{
		Current:   current,
		statePath: agentStatePath,
		opts:      opts,
	}

	st, err := loadAgentState(agentStatePath)
	if err != nil {
		slog.Warn("read agent state failed; persona migration disabled this boot",
			"component", "agent", "error", err)
		return pm
	}

	pm.Prev = st.previousRuntime()
	switch {
	case pm.Prev == "":
		pm.firstBoot = true
	case pm.Prev == current:
		// nothing
	default:
		if dir, ok := migrationDirection(pm.Prev, current); ok {
			pm.Needed = true
			pm.Direction = dir
		} else {
			slog.Info("agent runtime switched but no persona migrator for this pair; skipping",
				"component", "agent", "from", pm.Prev, "to", current)
		}
	}
	return pm
}

// Reconcile runs persona+memory migration after a runtime change, then records the new runtime. Logs errors and never blocks startup; failed migrations are retried on next boot.
func (p *PersonaMigration) Reconcile() {
	if p.firstBoot {
		if err := appendAgentRuntime(p.statePath, p.Current); err != nil {
			slog.Warn("seed agent state failed", "component", "agent", "error", err)
			return
		}
		slog.Info("agent state baseline recorded", "component", "agent", "runtime", p.Current)
		return
	}

	if p.Needed {
		slog.Info("agent runtime switched — migrating persona + memory",
			"component", "agent", "from", p.Prev, "to", p.Current, "direction", string(p.Direction))
		rep, err := migratepersona.Run(p.Direction, p.opts)
		switch {
		case err != nil:
			slog.Error("persona migration failed to run; agent keeps its existing persona",
				"component", "agent", "from", p.Prev, "to", p.Current, "error", err)
			return
		case rep.Summary[migratepersona.StatusError] > 0:
			slog.Error("persona migration had write errors; agent keeps its existing persona for those",
				"component", "agent", "from", p.Prev, "to", p.Current,
				"migrated", rep.Summary[migratepersona.StatusMigrated],
				"skipped", rep.Summary[migratepersona.StatusSkipped],
				"error", rep.Summary[migratepersona.StatusError])
		case rep.Summary[migratepersona.StatusConflict] > 0:
			slog.Warn("persona migration skipped some items (target exists); agent keeps existing for those",
				"component", "agent", "from", p.Prev, "to", p.Current,
				"migrated", rep.Summary[migratepersona.StatusMigrated],
				"conflict", rep.Summary[migratepersona.StatusConflict])
		case rep.Summary[migratepersona.StatusMigrated] == 0:
			slog.Warn("persona migration found nothing to migrate (source files missing?); agent keeps its existing persona",
				"component", "agent", "from", p.Prev, "to", p.Current,
				"skipped", rep.Summary[migratepersona.StatusSkipped])
		default:
			slog.Info("persona migration complete", "component", "agent",
				"migrated", rep.Summary[migratepersona.StatusMigrated],
				"skipped", rep.Summary[migratepersona.StatusSkipped])
		}
	}

	if err := appendAgentRuntime(p.statePath, p.Current); err != nil {
		slog.Warn("record agent state failed", "component", "agent", "error", err)
	}
}
