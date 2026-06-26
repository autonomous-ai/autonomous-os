package agent

import (
	"encoding/json"
	"errors"
	"log/slog"
	"os"
	"path/filepath"

	"github.com/goccy/go-yaml"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/server/config"
)

// MCPReconcile clones the configured MCP connectors into the active runtime after a
// runtime switch, so a device that had Notion/Linear/… wired under OpenClaw keeps
// them after switching to Hermes (and vice versa).
//
// It mirrors ChannelReconcile: it runs once in the startup sequence, is gated by a
// persisted marker (config.MCPAppliedRuntime) so it fires only when the runtime
// actually changed, and never blocks startup. Unlike persona migration — which
// reads/writes SOUL/MEMORY — the MCP servers live in each runtime's gateway config
// (OpenClaw `openclaw.json` `mcp.servers`, Hermes `config.yaml` `mcp_servers`), and
// each entry is self-contained (the auth header carries the token inline), so the
// clone is a pure config→config copy: read the PREVIOUS runtime's entries from disk,
// normalize to the canonical (OpenClaw-shaped) entry, and re-push each through the
// now-active gateway's WriteMCPEntry (which translates to its own native shape and
// restarts).
//
// Only the device-managed runtimes (openclaw, hermes) carry MCP config; a switch
// to/from an external runtime (picoclaw) reads/clones nothing.
type MCPReconcile struct {
	cfg *config.Config
	gw  domain.AgentGateway
}

// ProvideMCPReconcile is the Wire provider. It takes the resolved gateway so the
// clone targets the runtime that is actually active now.
func ProvideMCPReconcile(cfg *config.Config, gw domain.AgentGateway) *MCPReconcile {
	return &MCPReconcile{cfg: cfg, gw: gw}
}

// Reconcile clones the previous runtime's MCP servers into the current runtime when
// the runtime changed since the last clone, then advances the marker. A no-op when
// the runtime is unchanged. Never blocks startup; a transient clone failure leaves
// the marker un-advanced so the next boot retries.
func (r *MCPReconcile) Reconcile() {
	current := r.cfg.AgentRuntime
	if current == "" {
		current = domain.AgentRuntimeOpenClaw
	}
	if r.cfg.MCPAppliedRuntime == current {
		return // no switch since MCP was last cloned
	}

	// First observation (marker never set — e.g. the boot that introduced this
	// field): the MCP servers already live in the current runtime's config, so
	// record the baseline WITHOUT cloning. Re-clone only happens on an OBSERVED
	// switch (marker set to a different runtime).
	if r.cfg.MCPAppliedRuntime == "" {
		if err := r.cfg.WithLockSave(func(c *config.Config) { c.MCPAppliedRuntime = current }); err != nil {
			slog.Warn("mcp reconcile: record baseline failed", "component", "agent", "error", err)
			return
		}
		slog.Info("mcp reconcile: baseline recorded (no clone)", "component", "agent", "runtime", current)
		return
	}

	prev := r.cfg.MCPAppliedRuntime
	entries, err := readMCPEntries(prev, r.cfg)
	if err != nil {
		slog.Error("mcp reconcile: read previous runtime MCP failed; leaving marker for next-boot retry",
			"component", "agent", "from", prev, "to", current, "error", err)
		return
	}
	if len(entries) == 0 {
		slog.Info("mcp reconcile: runtime changed, no MCP servers to clone",
			"component", "agent", "from", prev, "to", current)
		if err := r.cfg.WithLockSave(func(c *config.Config) { c.MCPAppliedRuntime = current }); err != nil {
			slog.Warn("mcp reconcile: persist marker failed", "component", "agent", "error", err)
		}
		return
	}

	slog.Info("mcp reconcile: runtime changed, cloning MCP servers",
		"component", "agent", "from", prev, "to", current, "count", len(entries))
	cloneErr := false
	for name, entry := range entries {
		if err := r.gw.WriteMCPEntry(name, entry); err != nil {
			slog.Error("mcp reconcile: clone entry failed; will retry next boot",
				"component", "agent", "connector", name, "to", current, "error", err)
			cloneErr = true
			continue
		}
		slog.Info("mcp reconcile: cloned MCP server", "component", "agent", "connector", name, "runtime", current)
	}

	// Advance the marker ONLY on a clean pass. A transient WriteMCPEntry failure
	// leaves the marker un-advanced so the next boot re-runs the full clone (the
	// previous runtime's config is still on disk — neither switch wipes the other's).
	if cloneErr {
		slog.Warn("mcp reconcile: clone error — leaving marker for next-boot retry",
			"component", "agent", "runtime", current)
		return
	}
	if err := r.cfg.WithLockSave(func(c *config.Config) { c.MCPAppliedRuntime = current }); err != nil {
		slog.Warn("mcp reconcile: persist marker failed", "component", "agent", "error", err)
	}
}

// readMCPEntries reads the MCP server entries from a runtime's on-disk gateway
// config and returns them as canonical (OpenClaw-shaped) entries keyed by server
// name. Returns an empty map (not an error) when the config file or its MCP section
// is absent, so a runtime with no connectors reconciles to a clean no-op.
func readMCPEntries(runtime string, cfg *config.Config) (map[string]map[string]any, error) {
	switch runtime {
	case domain.AgentRuntimeOpenClaw:
		return readOpenclawMCP(filepath.Join(cfg.OpenclawConfigDir, "openclaw.json"))
	case domain.AgentRuntimeHermes:
		return readHermesMCP(filepath.Join(hermesHome, "config.yaml"))
	default:
		// External / non-device-managed runtimes (picoclaw): no MCP config to clone.
		return nil, nil
	}
}

// readOpenclawMCP reads openclaw.json `mcp.servers`. Entries are already in the
// canonical shape ({type:"http", url, headers} or {command, args, env}), so they
// pass through unchanged.
func readOpenclawMCP(path string) (map[string]map[string]any, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	var data map[string]any
	if err := json.Unmarshal(raw, &data); err != nil {
		return nil, err
	}
	mcp, _ := data["mcp"].(map[string]any)
	servers, _ := mcp["servers"].(map[string]any)
	out := make(map[string]map[string]any, len(servers))
	for name, v := range servers {
		if m, ok := v.(map[string]any); ok {
			out[name] = m
		}
	}
	return out, nil
}

// readHermesMCP reads config.yaml `mcp_servers` and normalizes each entry to the
// canonical OpenClaw shape: the Hermes-only `enabled` flag is dropped and an
// explicit `type: "http"` is re-added to url-bearing (hosted) servers, since
// OpenClaw selects the transport from that discriminator rather than url-vs-command.
func readHermesMCP(path string) (map[string]map[string]any, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	var data map[string]any
	if err := yaml.Unmarshal(raw, &data); err != nil {
		return nil, err
	}
	servers, _ := data["mcp_servers"].(map[string]any)
	out := make(map[string]map[string]any, len(servers))
	for name, v := range servers {
		if m, ok := v.(map[string]any); ok {
			out[name] = hermesToCanonicalMCP(m)
		}
	}
	return out, nil
}

// hermesToCanonicalMCP converts one Hermes mcp_servers entry into the canonical
// OpenClaw shape. Inverse of hermes.toHermesMCPEntry.
func hermesToCanonicalMCP(m map[string]any) map[string]any {
	out := make(map[string]any, len(m))
	for k, v := range m {
		if k == "enabled" {
			continue // Hermes-only activation flag; OpenClaw has no equivalent
		}
		out[k] = v
	}
	if _, hasType := out["type"]; !hasType {
		if _, hasURL := out["url"]; hasURL {
			out["type"] = "http"
		}
	}
	return out
}
