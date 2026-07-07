package picoclaw

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
)

// PicoClaw observer hook. Analogue of the Hermes os-server-observer hook
// (internal/hermes/hooks.go): it makes channel (Telegram) turns visible in the
// device Flow Monitor and drives [HW:/…] markers, by forwarding each turn to the
// loopback /api/agent/channel-turn endpoint that the shared ChannelTurn handler
// (server/agent/delivery/http/handler_channel_turn.go) already serves for Hermes.
//
// Two differences from Hermes:
//  1. Transport — PicoClaw process hooks are a subprocess speaking NDJSON JSON-RPC
//     over stdio (see resources/hooks/os-server-observer/observer.py), not an
//     in-process Python `handle()` discovered by directory scan.
//  2. Registration — PicoClaw does NOT scan a hooks dir; a hook is registered by a
//     config.json entry under hooks.processes.<name> (gated by hooks.enabled, which
//     defaults false — same shape as the tools.mcp gate). So os-server both
//     materializes the script AND patches config.json, then restarts the gateway.

//go:embed resources/hooks/os-server-observer/observer.py
var observerHookScript []byte

const (
	observerHookName       = "os-server-observer"
	observerHookDir        = "/root/.picoclaw/hooks/os-server-observer"
	observerHookScriptPath = observerHookDir + "/observer.py"
	observerHookURLMark    = "__OS_SERVER_TURN_URL__"
	observerHookPort       = 5000 // fallback when config.HttpPort is unset (matches os-server default)
)

// ensureObserverHook materializes observer.py into the PicoClaw hooks dir (with the
// loopback URL substituted) and registers it in config.json. Returns true when the
// script OR the config changed — the gateway loads hooks only at start, so the
// caller (EnsureOnboarding) must restart it when this reports changed. Idempotent:
// an unchanged boot rewrites nothing and reports false. Best-effort by contract:
// EnsureOnboarding logs and continues on error so a hook problem can't block boot.
func (s *PicoclawService) ensureObserverHook() (bool, error) {
	url := s.observerHookURL()
	script := bytes.ReplaceAll(observerHookScript, []byte(observerHookURLMark), []byte(url))

	if err := os.MkdirAll(observerHookDir, 0o755); err != nil {
		return false, fmt.Errorf("mkdir hook dir: %w", err)
	}
	changedScript, err := writePicoFileIfChanged(observerHookScriptPath, script, 0o755)
	if err != nil {
		return false, err
	}

	changedCfg, err := s.ensureObserverHookConfig(url)
	if err != nil {
		return false, err
	}
	if changedScript || changedCfg {
		slog.Info("picoclaw observer hook materialized", "component", "picoclaw", "dir", observerHookDir, "url", url)
		return true, nil
	}
	return false, nil
}

// observerHookURL is the loopback ChannelTurn endpoint the subprocess POSTs to.
func (s *PicoclawService) observerHookURL() string {
	port := s.config.HttpPort
	if port == 0 {
		port = observerHookPort
	}
	return fmt.Sprintf("http://127.0.0.1:%d/api/agent/channel-turn", port)
}

// ensureObserverHookConfig upserts hooks.processes.os-server-observer in config.json
// and asserts hooks.enabled=true (gate defaults false, like tools.mcp.enabled). The
// read-modify-write is serialized under mcpMu — the shared guard for config.json RMW
// (see mcp.go). Returns true only when the file actually changed, so a steady boot
// forces no gateway restart.
func (s *PicoclawService) ensureObserverHookConfig(url string) (bool, error) {
	s.mcpMu.Lock()
	defer s.mcpMu.Unlock()

	path := picoclawConfigPath()
	cfg, err := readPicoclawConfig(path)
	if err != nil {
		return false, err
	}
	before, _ := json.Marshal(cfg)

	applyObserverHook(cfg, observerHookScriptPath, url)

	after, _ := json.Marshal(cfg)
	if bytes.Equal(before, after) {
		return false, nil
	}
	if err := writePicoclawConfig(path, cfg); err != nil {
		return false, err
	}
	slog.Info("picoclaw observer hook registered in config.json", "component", "picoclaw", "hook", observerHookName)
	return true, nil
}

// applyObserverHook upserts hooks.processes.os-server-observer in the decoded config
// map and asserts the hooks.enabled gate. Pure map mutation — no I/O — so the nesting
// + gate rules are unit-testable without the hardcoded config path (mirrors
// applyMCPServerWrite in mcp.go).
func applyObserverHook(cfg map[string]any, scriptPath, url string) {
	hooks := ensurePicoMap(cfg, "hooks")
	hooks["enabled"] = true // global gate — must be on to load ANY hook
	procs := ensurePicoMap(hooks, "processes")
	procs[observerHookName] = map[string]any{
		"enabled":   true,
		"transport": "stdio",
		"command":   []any{"python3", scriptPath},
		"env": map[string]any{
			"OS_SERVER_TURN_URL": url,
			"OBSERVER_DEBUG":     "1",
		},
		"observe": []any{"turn_start", "turn_end"},
	}
}

// writePicoFileIfChanged writes data to path (with perm) only when it differs from
// the current content, so a steady boot neither churns the file nor forces a gateway
// restart. Returns true when the file was (re)written. Mirrors hermes.writeIfChanged
// but carries an explicit mode (observer.py is spawned, so keep it 0755).
func writePicoFileIfChanged(path string, data []byte, perm os.FileMode) (bool, error) {
	if cur, err := os.ReadFile(path); err == nil && bytes.Equal(cur, data) {
		return false, nil
	}
	if err := os.WriteFile(path, data, perm); err != nil {
		return false, fmt.Errorf("write %s: %w", filepath.Base(path), err)
	}
	return true, nil
}
