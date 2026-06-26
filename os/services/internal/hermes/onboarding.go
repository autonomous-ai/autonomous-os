package hermes

import (
	"context"
	"crypto/sha256"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"go.autonomous.ai/os/domain"
)

const (
	hermesConfigYAML  = "/root/.hermes/config.yaml"
	hermesEnvFile     = "/root/.hermes/.env"
	hermesGatewayUnit = "hermes-gateway"
)

// SetupAgent materializes the Hermes device config from config.json by running the
// same presync EnsureOnboarding runs. The device setup flow calls this AFTER it
// persists config.json (internal/device/service.go), so presync picks up the
// freshly-entered llm_api_key/base_url + channel tokens right away instead of
// waiting for the next os-server boot. The SetupRequest is unused — config.json
// (just saved) is the source of truth presync reads.
func (s *HermesService) SetupAgent(_ domain.SetupRequest) error {
	return s.EnsureOnboarding()
}

// EnsureOnboarding reconciles the device-side Hermes config on every os-server
// boot by running the embedded presync hook (PresyncScript) — the SAME script
// switch-runtime runs right before hermes starts.
//
// Why this is not a no-op (unlike the original stub's "user has confirmed Hermes
// is provisioned" assumption): presync was ONLY ever triggered by an explicit
// runtime SWITCH (UpdateAgentRuntime, old != new). So two paths kept a stale
// config.yaml that never picked up config.json's real llm_api_key/base_url:
//  1. a device that boots straight into hermes (DEVICE.md gateway.default: hermes,
//     or imaged with it) WITHOUT ever switching from openclaw;
//  2. an llm_* change while hermes was already active (the config-change listener
//     only refreshes HAL, not config.yaml).
//
// OpenClaw self-heals its config every boot (ensureAgentDefaults + StartModelSync);
// this gives Hermes the same property by reusing its own presync — no duplicated
// sync logic in Go.
//
// presync.sh is idempotent (yq fill-if-missing + sync, guarded skill restore), so
// a steady boot writes nothing. We hash config.yaml around the run and restart
// hermes-gateway ONLY when it actually changed, so there is no restart loop.
func (s *HermesService) EnsureOnboarding() error {
	// Hash both config.yaml AND .env: presync writes channel tokens to .env, so a
	// channel-only change (e.g. adding Slack) leaves config.yaml untouched and must
	// still trigger a gateway restart for the Hermes server to pick the channel up.
	before := fileHash(hermesConfigYAML) + fileHash(hermesEnvFile)

	if err := s.runPresync(); err != nil {
		return fmt.Errorf("hermes presync: %w", err)
	}
	// config "changed" covers config.yaml AND .env: presync writes channel tokens to
	// .env, so a channel-only change (e.g. adding Slack) leaves config.yaml untouched
	// and must still restart the gateway for the Hermes server to pick the channel up.
	configChanged := fileHash(hermesConfigYAML)+fileHash(hermesEnvFile) != before

	// Materialize the os-server-observer hook so channel turns surface in Flow
	// Monitor. Best-effort: a hook write failure must not block the boot path
	// (config self-heal above already succeeded).
	hookChanged, err := s.ensureObserverHook()
	if err != nil {
		slog.Warn("hermes observer hook materialize failed", "component", "hermes", "error", err)
	}

	// Restart the gateway only when config.yaml/.env OR the hook actually changed —
	// all are loaded only at gateway start, so an unchanged boot is a no-op.
	skillsRestored := s.ensureSkills()

	if !configChanged && !hookChanged && !skillsRestored {
		slog.Info("hermes onboarding: config + hooks + skills unchanged, no restart", "component", "hermes")
		return nil
	}

	slog.Info("hermes onboarding: change detected, restarting gateway",
		"component", "hermes", "unit", hermesGatewayUnit,
		"config_changed", configChanged, "hook_changed", hookChanged, "skills_restored", skillsRestored)
	if err := restartHermesGateway(); err != nil {
		// Non-fatal: the new config/hook is on disk; the gateway picks it up on its
		// next (re)start even if this one failed. Don't block the os-server boot path.
		slog.Warn("hermes gateway restart failed", "component", "hermes", "error", err)
	}
	return nil
}

// runPresync materializes the embedded presync script to a temp file and runs it.
// The script is self-contained (hardcodes /root/.hermes + /root/config/config.json)
// and idempotent, so it is safe to run on every boot.
func (s *HermesService) runPresync() error {
	f, err := os.CreateTemp("", "hermes-presync-*.sh")
	if err != nil {
		return fmt.Errorf("create temp: %w", err)
	}
	path := f.Name()
	defer os.Remove(path)
	if _, err := f.Write(PresyncScript); err != nil {
		f.Close()
		return fmt.Errorf("write script: %w", err)
	}
	f.Close()
	if err := os.Chmod(path, 0o755); err != nil {
		return fmt.Errorf("chmod: %w", err)
	}

	// Generous timeout: a normal boot is fast (yq edits only), but a post-reset
	// boot may run `claw migrate` to restore skills (see presync.sh §0).
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()
	out, err := exec.CommandContext(ctx, "bash", path).CombinedOutput()
	if len(out) > 0 {
		slog.Info("hermes presync output", "component", "hermes", "output", strings.TrimSpace(string(out)))
	}
	if err != nil {
		return fmt.Errorf("run presync: %w", err)
	}
	return nil
}

// fileHash returns a content hash of path, or "" when the file is absent — so a
// config.yaml that did not exist before and was created by presync reads as
// changed (triggering the restart).
func fileHash(path string) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(b)
	return string(sum[:])
}

// RestartAgent restarts the hermes gateway only. Mirrors
// internal/openclaw/service_setup.go RestartAgent (which restarts the openclaw
// gateway) — same contract, different unit.
func (s *HermesService) RestartAgent() error {
	slog.Debug("restarting hermes gateway", "component", "hermes")
	if err := restartHermesGateway(); err != nil {
		return err
	}
	slog.Info("restart completed", "component", "hermes")
	return nil
}

// ensureSkills downloads all supported skills from CDN into
// ~/.hermes/skills/openclaw-imports/ when the directory is absent or empty.
// This covers two cases where skills are missing without a CDN version bump:
//   - factory reset (wipes openclaw-imports/) with Hermes as the active runtime
//   - first boot as Hermes when claw migrate had nothing to copy from
//
// Steady-state updates are handled by StartSkillWatcher (version-gated CDN
// polling). This is intentionally a restore guard, not a full sync — we only
// act when the dir is empty so we don't double-download on every boot.
//
// Returns true when skills were restored so EnsureOnboarding includes a gateway
// restart — the running Hermes instance would otherwise not load skills that
// landed on disk after it started.
func (s *HermesService) ensureSkills() bool {
	skillsDir := filepath.Join(hermesHome, "skills", "openclaw-imports")
	entries, err := os.ReadDir(skillsDir)
	if err == nil && len(entries) > 0 {
		return false // skills present — watcher handles updates
	}
	names := s.supportedSkills()
	if len(names) == 0 {
		return false
	}
	slog.Info("hermes onboarding: skills dir empty — restoring from CDN",
		"component", "hermes", "count", len(names))
	changed := s.downloadSkillsByName(names)
	slog.Info("hermes onboarding: skills restored", "component", "hermes", "restored", len(changed))
	return len(changed) > 0
}

// restartHermesGateway bounces the hermes daemon so it reloads config.yaml. The
// unit name (hermes-gateway) is declared by install.sh for switch-runtime.
func restartHermesGateway() error {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "systemctl", "restart", hermesGatewayUnit).CombinedOutput()
	if err != nil {
		return fmt.Errorf("systemctl restart %s: %s: %w", hermesGatewayUnit, strings.TrimSpace(string(out)), err)
	}
	return nil
}
