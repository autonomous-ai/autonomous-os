package picoclaw

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
)

// PicoClaw MCP wiring. Unlike OpenClaw (top-level mcp.servers in openclaw.json) and
// Hermes (top-level mcp_servers in config.yaml), PicoClaw nests servers under
// tools.mcp.servers in /root/.picoclaw/config.json, and the whole subsystem is gated
// by tools.mcp.enabled which defaults to FALSE — so an unqualified write is silently
// ignored. Both facts are handled here; see docs/agentic/picoclaw.md §MCP.

// picoclawConfigPath returns PicoClaw's structure config.json — the parent of the
// workspace dir (HOME=/root → /root/.picoclaw/config.json). Secrets live in
// .security.yml, which we never touch here.
func picoclawConfigPath() string {
	return filepath.Join(filepath.Dir(picoclawWorkspaceDir), "config.json")
}

// WriteMCPEntry upserts tools.mcp.servers.<name> in config.json and restarts the
// gateway so PicoClaw picks up the server. entry is the canonical (OpenClaw-shaped)
// server-config map the connector writer produces — {type:"http", url, headers} for
// hosted MCP, or {command, args, env} for stdio.
//
// Two PicoClaw-specific rules are enforced (see file header):
//  1. The entry lands under tools.mcp.servers, created if absent.
//  2. tools.mcp.enabled is asserted true — otherwise the server is silently ignored.
//
// The entry is passed through with enabled:true asserted (PicoClaw's per-server active
// flag). The type key is KEPT verbatim: PicoClaw's transport values (stdio/sse/http)
// already match the OpenClaw shape, and an explicit type:"http" avoids PicoClaw's
// empty-type→sse inference. The read-modify-write cycle is serialized under mcpMu.
func (s *PicoclawService) WriteMCPEntry(name string, entry map[string]any) error {
	s.mcpMu.Lock()
	defer s.mcpMu.Unlock()

	path := picoclawConfigPath()
	cfg, err := readPicoclawConfig(path)
	if err != nil {
		return err
	}

	applyMCPServerWrite(cfg, name, entry)

	if err := writePicoclawConfig(path, cfg); err != nil {
		return err
	}
	slog.Info("[mcp] wrote tools.mcp.servers entry", "component", "picoclaw", "connector", name)

	if err := restartPicoclawGateway(); err != nil {
		slog.Warn("[mcp] restart gateway after mcp entry write", "component", "picoclaw", "err", err)
	}
	return nil
}

// RemoveMCPEntry deletes tools.mcp.servers.<name> from config.json. Returns
// removed=false (no write, no restart) when the entry was already absent or the
// config file does not exist yet. tools.mcp.enabled is left on — other servers may
// still be wired, and an enabled-but-empty MCP block loads nothing. Mirrors
// OpenclawService.RemoveMCPEntry.
func (s *PicoclawService) RemoveMCPEntry(name string) (bool, error) {
	s.mcpMu.Lock()
	defer s.mcpMu.Unlock()

	path := picoclawConfigPath()
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return false, nil
		}
		return false, fmt.Errorf("read picoclaw config: %w", err)
	}
	var cfg map[string]any
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return false, fmt.Errorf("parse picoclaw config: %w", err)
	}

	if !applyMCPServerRemove(cfg, name) {
		return false, nil
	}

	if err := writePicoclawConfig(path, cfg); err != nil {
		return false, err
	}
	slog.Info("[mcp] removed tools.mcp.servers entry", "component", "picoclaw", "connector", name)

	if err := restartPicoclawGateway(); err != nil {
		slog.Warn("[mcp] restart gateway after mcp entry remove", "component", "picoclaw", "err", err)
	}
	return true, nil
}

// applyMCPServerWrite upserts tools.mcp.servers.<name> in the decoded config map and
// asserts the tools.mcp.enabled gate (default false). Pure map mutation — no I/O — so
// the nesting + gate rules are unit-testable without the hardcoded config path.
func applyMCPServerWrite(cfg map[string]any, name string, entry map[string]any) {
	tools := ensurePicoMap(cfg, "tools")
	mcp := ensurePicoMap(tools, "mcp")
	mcp["enabled"] = true // global gate defaults to false — must be on to load any server
	servers := ensurePicoMap(mcp, "servers")
	servers[name] = toPicoclawMCPEntry(entry)
}

// applyMCPServerRemove deletes tools.mcp.servers.<name> from the decoded config map.
// Returns false (no mutation) when the tools.mcp.servers path or the named entry is
// absent. tools.mcp.enabled is deliberately left on — other servers may remain.
func applyMCPServerRemove(cfg map[string]any, name string) bool {
	tools, _ := cfg["tools"].(map[string]any)
	mcp, _ := tools["mcp"].(map[string]any)
	servers, _ := mcp["servers"].(map[string]any)
	if servers == nil {
		return false
	}
	if _, ok := servers[name]; !ok {
		return false
	}
	delete(servers, name)
	return true
}

// toPicoclawMCPEntry copies the canonical OpenClaw-shaped server entry and asserts
// enabled:true (PicoClaw's per-server active flag; also re-enables a previously
// disabled entry on re-write). Unlike Hermes' translator the type key is kept: it
// maps 1-for-1 onto PicoClaw's transport field and guards against the empty-type→sse
// default. url/headers (http) and command/args/env (stdio) pass through unchanged.
func toPicoclawMCPEntry(entry map[string]any) map[string]any {
	out := make(map[string]any, len(entry)+1)
	for k, v := range entry {
		out[k] = v
	}
	out["enabled"] = true
	return out
}

// readPicoclawConfig loads config.json into a generic map. Errors (including
// not-exist) are returned so connector writes surface a clear failure rather than
// silently no-op'ing on an un-provisioned device.
func readPicoclawConfig(path string) (map[string]any, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read picoclaw config: %w", err)
	}
	cfg := map[string]any{}
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return nil, fmt.Errorf("parse picoclaw config: %w", err)
	}
	return cfg, nil
}

// writePicoclawConfig marshals + atomically writes config.json. PicoClaw runs as root
// and owns /root/.picoclaw, so no chown is needed (unlike openclaw's runtime user).
// presync.sh re-asserts channel_list/model_list structure idempotently on the next
// boot, so any key reordering from the map marshal self-heals.
func writePicoclawConfig(path string, cfg map[string]any) error {
	written, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal picoclaw config: %w", err)
	}
	if err := atomicWritePicoFile(path, written, 0o644); err != nil {
		return fmt.Errorf("write picoclaw config: %w", err)
	}
	return nil
}

// ensurePicoMap returns parent[key] as a map[string]any, creating it when absent or
// of the wrong type. Mirrors openclaw.ensureMap for the JSON-decoded config tree.
func ensurePicoMap(parent map[string]any, key string) map[string]any {
	if existing, ok := parent[key].(map[string]any); ok && existing != nil {
		return existing
	}
	created := map[string]any{}
	parent[key] = created
	return created
}

// atomicWritePicoFile writes data to a temp file in the same dir then renames it over
// path, so a crash mid-write never leaves a truncated config.json. Mirrors
// hermes.atomicWriteFile (kept local to avoid a cross-package dependency); no chown
// because PicoClaw runs as root.
func atomicWritePicoFile(path string, data []byte, perm os.FileMode) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), ".picoclaw-*.tmp")
	if err != nil {
		return fmt.Errorf("create temp file: %w", err)
	}
	tmpPath := tmp.Name()
	cleanup := func() { _ = os.Remove(tmpPath) }

	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		cleanup()
		return fmt.Errorf("write temp file: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		cleanup()
		return fmt.Errorf("fsync temp file: %w", err)
	}
	if err := tmp.Close(); err != nil {
		cleanup()
		return fmt.Errorf("close temp file: %w", err)
	}
	if err := os.Chmod(tmpPath, perm); err != nil {
		cleanup()
		return fmt.Errorf("chmod temp file: %w", err)
	}
	if err := os.Rename(tmpPath, path); err != nil {
		cleanup()
		return fmt.Errorf("rename temp file: %w", err)
	}
	return nil
}
