package config

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
)

// AgentRuntimeValue reads the selection while an external activation may save it.
func (c *Config) AgentRuntimeValue() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.AgentRuntime
}

// RuntimeSelectionStatus snapshots the saved selection once. Readiness belongs
// to the instantiated gateway; saving a selection never activates that gateway.
func (c *Config) RuntimeSelectionStatus(active string, ready bool) map[string]any {
	selected := strings.ToLower(strings.TrimSpace(c.AgentRuntimeValue()))
	return map[string]any{
		"current": active, "active": active, "selected": selected,
		"ready": ready, "readiness_runtime": active,
		"restart_required": selected != "" && selected != strings.ToLower(strings.TrimSpace(active)),
	}
}

// SelectAgentRuntime atomically changes only the selection, preserving all raw
// config fields (including unknown fields). It does not normalize voice URLs,
// seed config, write a HAL snapshot or notify device lifecycle listeners.
// Memory changes only after persistence succeeds. Activation needs a process
// restart managed by the operator; this method controls no services.
func (c *Config) SelectAgentRuntime(runtime string) error {
	if runtime == "" {
		return errors.New("empty runtime selection")
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	raw, err := os.ReadFile(configPath)
	if err != nil {
		return err
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		return err
	}
	if fields == nil {
		return errors.New("config must be an object")
	}
	fields["agent_runtime"], _ = json.Marshal(runtime)
	raw, err = json.MarshalIndent(fields, "", "  ")
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(configPath), ".runtime-selection-*")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if _, err := f.Write(raw); err != nil {
		_ = f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	if err := os.Rename(f.Name(), configPath); err != nil {
		return err
	}
	c.AgentRuntime = runtime
	return nil
}
