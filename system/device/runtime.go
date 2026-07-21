package device

import (
	"bytes"
	_ "embed"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"strings"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

// switch_runtime.sh is the generic, backend-agnostic agentic-backend switcher.
// It is embedded in os-server and written to switchRuntimeBin on demand, so the
// switcher is versioned and OTA-updated together with the binary — no
// scripts/imager/setup.sh change is ever needed to ship or update it, and adding a new
// backend is just a runtimes/<name>/install.sh on the CDN.
//
//go:embed switch_runtime.sh
var switchRuntimeScript []byte

const switchRuntimeBin = "/usr/local/bin/switch-runtime"

// ensureSwitchRuntime writes the embedded switcher to switchRuntimeBin when it
// is missing or stale (content drift after an OTA). Idempotent — a no-op once
// the on-disk copy matches the embedded one.
func ensureSwitchRuntime() error {
	if cur, err := os.ReadFile(switchRuntimeBin); err == nil && bytes.Equal(cur, switchRuntimeScript) {
		return nil
	}
	if err := os.WriteFile(switchRuntimeBin, switchRuntimeScript, 0o755); err != nil {
		return fmt.Errorf("write %s: %w", switchRuntimeBin, err)
	}
	return nil
}

// frDefaultAgentPath is a build-time-baked, per-image default runtime — e.g.
// the intern-v2 case-color images (blue=hermes, orange=openclaw, black=claudecode,
// scripts/imager/build-orangepi.sh DEFAULT_AGENT). It deliberately lives outside
// /root/config/config.json so it survives Factory Reset untouched (it is NOT
// in factoryreset.go's deviceWipePaths — verified device-side: siblings like
// bootstrap.json/buddy.json in the same dir already survive F_R the same way).
// Most builds never write this file, so it is absent and this is a no-op.
// var, not const, so tests can point it at a temp file.
var frDefaultAgentPath = "/root/config/f_r_default_agent"

// readFRDefaultAgent returns the baked per-image default runtime, or "" if
// the file is absent (the common case — falls back to DEVICE.md gateway.default).
func readFRDefaultAgent() string {
	b, err := os.ReadFile(frDefaultAgentPath)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

// SeedAgentRuntimeFromGateway materializes the device's default runtime into
// config.agent_runtime when the field is still empty, then persists it. The
// default is resolved with priority: (1) the build-baked f_r_default_agent
// file — a per-image/per-color default that survives Factory Reset, so a
// reset device re-seeds to the SAME default it shipped with, not a
// device-type-wide value shared across every color variant; (2) DEVICE.md
// gateway.default, as before. Once a concrete value is on disk the device
// "owns" its runtime: a dev who set it (via switch or by hand) is left
// untouched, and the resolve-fallback in CurrentAgentRuntimeFromConfig becomes
// a no-op. Idempotent — only the first boot of a fresh/legacy config.json
// writes. When neither source names a valid runtime there is nothing to seed,
// so the field stays empty and the runtime keeps resolving to openclaw at boot.
func SeedAgentRuntimeFromGateway(cfg *config.Config) {
	if cfg == nil || strings.TrimSpace(cfg.AgentRuntime) != "" {
		return
	}
	g := strings.ToLower(readFRDefaultAgent())
	if g == "" || !domain.IsValidAgentRuntime(g) {
		g = strings.ToLower(strings.TrimSpace(GatewayDefault(cfg.DeviceTypeOrDefault())))
	}
	if g == "" || !domain.IsValidAgentRuntime(g) {
		return
	}
	cfg.AgentRuntime = g
	if err := cfg.Save(); err != nil {
		slog.Error("seed agent_runtime from gateway.default failed", "component", "device", "runtime", g, "error", err)
	}
}

// CurrentAgentRuntime returns the effective agentic backend, resolved the same
// way as system/agent/factory.go: config.agent_runtime, else the device's
// DEVICE.md gateway.default, else openclaw. Used by GET /api/device/agent-runtime
// so the web settings page shows what is actually running.
func (s *Service) CurrentAgentRuntime() string {
	return CurrentAgentRuntimeFromConfig(s.config)
}

// CurrentAgentRuntimeFromConfig resolves the effective agentic backend without a
// Service receiver, so callers holding only a *config.Config (e.g. the MQTT info
// handler) can report what is actually running. Same precedence as factory.go:
// config.agent_runtime, else DEVICE.md gateway.default, else openclaw.
func CurrentAgentRuntimeFromConfig(cfg *config.Config) string {
	if r := strings.ToLower(strings.TrimSpace(cfg.AgentRuntime)); r != "" {
		return r
	}
	if g := GatewayDefault(cfg.DeviceTypeOrDefault()); g != "" {
		return strings.ToLower(strings.TrimSpace(g))
	}
	return domain.AgentRuntimeOpenClaw
}

// UpdateAgentRuntime swaps the agentic backend (openclaw / hermes / picoclaw). It
// runs switch-runtime and BLOCKS until the switch finishes, then persists
// config.agent_runtime ONLY if it landed — so a failed install never leaves disk
// pointing at a backend that isn't actually running. Shared by the MQTT
// hermes.setup / picoclaw.setup handlers and the HTTP config API.
//
// Returns:
//   - (true, nil)  — the switch landed; the caller MUST report success and then
//     call RestartForAgentRuntime so factory.go re-resolves the gateway.
//   - (false, nil) — no-op (already on the target); no restart needed.
//   - (false, err) — the switch failed and was rolled back to `old`.
//
// It deliberately does NOT restart os-server: the restart kills os-server, so it
// has to happen AFTER the caller has put its success ack on the wire. switch-runtime
// no longer restarts os-server either (it used to) — that move is what lets the
// caller's goroutine survive long enough to ack the real result.
func (s *Service) UpdateAgentRuntime(d domain.AgentRuntimeSetData) (bool, error) {
	runtime := strings.ToLower(strings.TrimSpace(d.Runtime))
	// Reject unknown values outright. factory.go falls back to openclaw on
	// garbage, but an unknown runtime from the BFF/web is a contract error we
	// surface rather than silently coerce.
	if !domain.IsValidAgentRuntime(runtime) {
		return false, fmt.Errorf("invalid runtime %q (want %s)", d.Runtime, strings.Join(domain.AgentRuntimes, "|"))
	}

	// Resolve the currently-active runtime BEFORE the save so switch-runtime
	// knows which backend to stop. Default to openclaw (matches factory.go).
	old := strings.ToLower(strings.TrimSpace(s.config.AgentRuntime))
	if old == "" {
		old = domain.AgentRuntimeOpenClaw
	}

	// No-op guard: re-sending the active runtime shouldn't churn services or
	// bounce os-server. Returns switched=false so the caller skips the restart.
	if old == runtime {
		slog.Info("agent runtime unchanged, skipping switch", "component", "device", "runtime", runtime)
		return false, nil
	}

	// Make sure the embedded switcher is on disk before we depend on it, and
	// materialize the target's embedded installer (if compiled in) so the switch
	// works fully offline — switch-runtime runs the local copy, no CDN needed.
	if err := ensureSwitchRuntime(); err != nil {
		return false, fmt.Errorf("install switch-runtime: %w", err)
	}
	if err := materializeInstaller(runtime); err != nil {
		return false, fmt.Errorf("materialize %s installer: %w", runtime, err)
	}
	// Refresh the pre-start hook on disk too, so a plain os-server OTA delivers
	// its latest version (config self-heal) even when the backend is already
	// installed and install.sh is therefore skipped. switch-runtime runs it
	// right before the backend starts. Non-fatal: a backend without a presync, or
	// a transient write error, must not block the switch.
	if err := materializePresync(runtime); err != nil {
		slog.Warn("materialize presync hook failed (non-fatal)", "component", "device", "runtime", runtime, "error", err)
	}

	slog.Info("running switch-runtime", "component", "device", "from", old, "to", runtime)

	// Run the switcher and WAIT for its exit code. We deliberately do NOT persist
	// config.agent_runtime first: if the install/start fails, config.json stays at
	// `old`, so the device — including after a crash or reboot mid-switch — resolves
	// the still-installed old backend instead of a half-installed new one. On failure
	// switch-runtime has already rolled the systemd units back to `old`, and since we
	// never touched config (memory or disk) there is nothing to revert.
	if err := s.runSwitchRuntime(runtime, old); err != nil {
		return false, fmt.Errorf("switch to %s failed, rolled back to %s: %w", runtime, old, err)
	}

	// Switch landed (NEW up, OLD stopped) — only NOW persist the new runtime, so the
	// imminent RestartForAgentRuntime (and every future boot) resolves it. If this
	// save fails the units are already on NEW while disk still says `old`; surface the
	// error so the caller skips the restart and an operator can re-trigger.
	if err := s.config.WithLockSave(func(c *config.Config) {
		c.AgentRuntime = runtime
	}); err != nil {
		return false, fmt.Errorf("switch to %s landed but persisting agent_runtime failed: %w", runtime, err)
	}

	slog.Info("agent runtime switch landed", "component", "device", "from", old, "to", runtime)
	return true, nil
}

// runSwitchRuntime runs the embedded switcher in a transient systemd unit and
// blocks for its result. --wait propagates switch-runtime's exit code (so we learn
// landed-vs-rolled-back), --collect GCs the unit afterwards. Pass <new> <old> so
// the switch stays fully generic (no hardcoded backend list anywhere). Safe to wait
// on from this process because switch-runtime no longer restarts os-server.
func (s *Service) runSwitchRuntime(newRuntime, oldRuntime string) error {
	if err := exec.Command("systemd-run", "--quiet", "--collect", "--wait",
		"--unit=os-runtime-switch", switchRuntimeBin, newRuntime, oldRuntime).Run(); err != nil {
		return fmt.Errorf("switch-runtime exited non-zero (see `journalctl -u os-runtime-switch`): %w", err)
	}
	return nil
}

// RestartForAgentRuntime restarts os-server so system/agent/factory.go re-resolves
// the gateway against the freshly-persisted runtime. The restart runs in its OWN
// transient systemd unit (systemd-run) — NOT inline: `systemctl restart os-server`
// tears down os-server's cgroup, which would SIGTERM a plain child mid-call
// ("signal: terminated") and is racy. systemd-run launches it in a separate cgroup
// that survives the teardown, then returns once the unit is started (before the
// teardown begins), so this returns cleanly and real launch failures still surface.
//
// The unit is named (--unit) and NOT --quiet on purpose: this is the one action that
// bounces the whole server, so we want systemd-run to log which unit ran it and to
// be able to `journalctl -u os-server-runtime-restart` after the fact. --collect GCs
// the unit once it's done. Callers MUST have already published their success ack —
// this kills os-server.
func (s *Service) RestartForAgentRuntime() error {
	if err := exec.Command("systemd-run", "--collect", "--unit=os-server-runtime-restart",
		"systemctl", "restart", "os-server").Run(); err != nil {
		return fmt.Errorf("spawn os-server restart: %w", err)
	}
	return nil
}
