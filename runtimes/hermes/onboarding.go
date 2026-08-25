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

	"go.autonomous.ai/os/system/domain"
)

const (
	hermesConfigYAML  = "/root/.hermes/config.yaml"
	hermesEnvFile     = "/root/.hermes/.env"
	hermesGatewayUnit = "hermes-gateway"

	// soulOSMarker delimits the OS-managed block in SOUL.md so it can be stripped +
	// re-injected cleanly on update. Same marker text as the openclaw/picoclaw
	// AGENTS.md blocks (osMandatoryMarker there).
	soulOSMarker = "<!-- OS DO NOT REMOVE -->"

	// soulSkillPriorityBlock is the OS-managed skill-priority block kept at the end
	// of ~/.hermes/SOUL.md. OpenClaw/PicoClaw carry their skill rules in an
	// OS-managed AGENTS.md block, but Hermes has no AGENTS.md slot — SOUL.md is the
	// one prompt file it loads every session — so the rule rides in the soul.
	// Without it Hermes weighs its own bundled skills as equals of the device's
	// platform skills and can pick e.g. a bundled email skill (installing himalaya)
	// for "send an email" while the connectors skill already has the device's Gmail
	// credentials on disk. Marker-delimited so ensureSoulSkillPriorityBlock can
	// strip + re-append it: an os-server OTA refreshes the wording, and a
	// `claw migrate` that rewrites SOUL.md (presync §0) self-heals next boot.
	soulSkillPriorityBlock = soulOSMarker + `
**Skill priority (MANDATORY):** The skills under ` + "`skills/openclaw-imports/`" + ` are this device's built-in platform skills. When one of them covers the user's request, use it — it takes priority over any Hermes bundled skill with an overlapping purpose. In particular, anything on a connected third-party service (Gmail, Google Calendar, Google Drive, Notion, Figma, Asana, Linear, GitHub, …) — reading, sending, or acting — goes through the ` + "`connectors`" + ` skill: the device's credentials are already on disk there. Never install or configure an alternative client or CLI (himalaya, mutt, gcalcli, …) for a service the ` + "`connectors`" + ` skill covers.

**Silence = the literal token ` + "`NO_REPLY`" + `.** When a skill says not to speak, output exactly ` + "`NO_REPLY`" + ` and nothing else. Never narrate the decision ("Sound event, no user message. Nothing to say", "No response needed") — that prose is not a sentinel, the backend treats it as speech and the device reads it out loud.
---`
)

// SetupAgent materializes the Hermes device config from config.json by running the
// same presync EnsureOnboarding runs. The device setup flow calls this AFTER it
// persists config.json (system/device/setup.go), so presync picks up the
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
//  1. a device that boots straight into hermes (ROBOT.md gateway.default: hermes,
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

	// Presync is best-effort: a failure must not block gateway startup. A device
	// that just had config.json written (setup wizard, first boot) needs the gateway
	// running even when presync hits a transient error (missing dep, bad env, etc.).
	// We log the error and continue so ensureGatewayUnit + restartHermesGateway still
	// fire below.
	if err := s.runPresync(); err != nil {
		slog.Warn("hermes presync failed, continuing with gateway start", "component", "hermes", "error", err)
	}

	// Keep the OS-managed skill-priority block in SOUL.md current. Runs AFTER
	// presync because its §0 claw-migrate can rewrite the soul. Best-effort and
	// not part of the restart decision below — SOUL.md is read per session, not
	// at gateway start (same rule UpdateIdentityName relies on).
	if _, err := s.ensureSoulSkillPriorityBlock(); err != nil {
		slog.Warn("hermes soul skill-priority block failed", "component", "hermes", "error", err)
	}

	// De-dupe "<name>-imported" skill dirs a claw migrate may have left behind
	// (also runs AFTER presync, whose §0 can run claw migrate). Two candidates
	// for one skill name make Hermes refuse to load the skill entirely.
	skillsDeduped := s.pruneImportedSkillDuplicates()

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

	// Reconcile every supported platform skill from the CDN, not only an empty
	// directory. This closes the restart race where OTA metadata is already new
	// when the watcher seeds its versions but the local skill files are old.
	// Content hashes make an unchanged boot a no-op.
	changedSkills := s.downloadSkills()
	skillsSynced := len(changedSkills) > 0

	// B (self-heal): make sure the hermes-gateway.service unit actually exists before
	// we rely on (re)starting it. A device that reached hermes WITHOUT switch-runtime
	// (e.g. a hand-edited config.json agent_runtime=hermes after factory reset) has the
	// pre-baked binary but no unit, so IsReady()'s /health probe — and the setup
	// WaitForAgentReady gate — would fail forever. Install it on demand (fast; binary
	// is pre-baked). Also (re)start when the unit exists but is not running: factory
	// reset disables+stops it (reset.go step 4, "SetupAgent re-enables"), and it can
	// crash on a stale config that presync just fixed.
	gatewayInstalled := s.ensureGatewayUnit()
	gatewayDown := !gatewayActive()

	if !configChanged && !hookChanged && !skillsSynced && !skillsDeduped && !gatewayInstalled && !gatewayDown {
		slog.Info("hermes onboarding: config + hooks + skills unchanged, gateway up — no restart", "component", "hermes")
		return nil
	}

	slog.Info("hermes onboarding: (re)starting gateway",
		"component", "hermes", "unit", hermesGatewayUnit,
		"config_changed", configChanged, "hook_changed", hookChanged, "skills_synced", skillsSynced,
		"skills_deduped", skillsDeduped, "gateway_installed", gatewayInstalled, "gateway_down", gatewayDown)
	// Re-enable so hermes survives a reboot — factory reset disabled the unit, and a
	// freshly installed one is not enabled for boot. Best-effort; restart still starts
	// it for this session even if enable fails.
	enableHermesGateway()
	if err := restartHermesGateway(); err != nil {
		// Non-fatal: the new config/hook is on disk; the gateway picks it up on its
		// next (re)start even if this one failed. Don't block the os-server boot path.
		slog.Warn("hermes gateway restart failed", "component", "hermes", "error", err)
	}

	// Notify after the restart attempt so the gateway has the best chance to be
	// connected. This matches OpenClaw's onboarding flow.
	s.notifySkillChanges(changedSkills)
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
// runtimes/openclaw/service_setup.go RestartAgent (which restarts the openclaw
// gateway) — same contract, different unit.
func (s *HermesService) RestartAgent() error {
	slog.Debug("restarting hermes gateway", "component", "hermes")
	if err := restartHermesGateway(); err != nil {
		return err
	}
	slog.Info("restart completed", "component", "hermes")
	return nil
}

// pruneImportedSkillDuplicates removes the "<name>-imported" duplicates that
// `hermes claw migrate --skill-conflict rename` leaves in skills/openclaw-imports
// when it runs while the canonical copies are already on disk (install-time race
// with the skill watcher, manual migrate runs, …). Two candidates for one skill
// name make Hermes' skill_view refuse to load the skill at all ("Ambiguous skill
// name") — the agent then improvises without it, which is how a device with a
// valid Gmail token ended up trying to install CLI email clients. The copy under
// <name> is canonical (the skill watcher keeps it fresh from the CDN), so the
// -imported duplicate is dropped; when only the -imported copy exists it is
// renamed to <name> instead. Returns true when anything changed so
// EnsureOnboarding restarts the gateway (the session skill index is built at
// gateway start).
func (s *HermesService) pruneImportedSkillDuplicates() bool {
	return pruneImportedDuplicatesIn(filepath.Join(hermesHome, "skills", "openclaw-imports")) > 0
}

// pruneImportedDuplicatesIn is the path-parameterized worker (split out for
// tests). Returns the number of entries it removed or renamed.
func pruneImportedDuplicatesIn(dir string) int {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return 0 // absent dir: nothing to prune
	}
	changed := 0
	for _, e := range entries {
		if !e.IsDir() || !strings.HasSuffix(e.Name(), "-imported") {
			continue
		}
		base := strings.TrimSuffix(e.Name(), "-imported")
		if base == "" {
			continue
		}
		dupPath := filepath.Join(dir, e.Name())
		basePath := filepath.Join(dir, base)
		if _, err := os.Stat(basePath); err == nil {
			if err := os.RemoveAll(dupPath); err != nil {
				slog.Warn("prune imported skill duplicate failed", "component", "hermes", "skill", e.Name(), "error", err)
				continue
			}
			slog.Info("pruned imported skill duplicate", "component", "hermes", "removed", e.Name(), "kept", base)
		} else {
			// Only copy on disk — reclaim the canonical name instead of deleting.
			if err := os.Rename(dupPath, basePath); err != nil {
				slog.Warn("rename imported skill failed", "component", "hermes", "skill", e.Name(), "error", err)
				continue
			}
			slog.Info("renamed imported skill to canonical name", "component", "hermes", "from", e.Name(), "to", base)
		}
		changed++
	}
	return changed
}

// ensureSoulSkillPriorityBlock reconciles the OS-managed skill-priority block in
// ~/.hermes/SOUL.md (see soulSkillPriorityBlock). Returns true when the file
// changed. Atomic tmp+rename like UpdateIdentityName so a mid-write crash can't
// truncate the soul; no gateway restart is needed — Hermes re-reads SOUL.md at
// the next session.
func (s *HermesService) ensureSoulSkillPriorityBlock() (bool, error) {
	soulPath := filepath.Join(hermesHome, "SOUL.md")
	raw, err := os.ReadFile(soulPath)
	if err != nil && !os.IsNotExist(err) {
		return false, fmt.Errorf("read %s: %w", soulPath, err)
	}
	updated := upsertSoulSkillPriorityBlock(string(raw))
	if updated == string(raw) {
		return false, nil
	}
	if err := os.MkdirAll(hermesHome, 0o755); err != nil {
		return false, fmt.Errorf("mkdir %s: %w", hermesHome, err)
	}
	tmp, err := os.CreateTemp(hermesHome, ".SOUL.*.tmp")
	if err != nil {
		return false, fmt.Errorf("create tmp: %w", err)
	}
	tmpPath := tmp.Name()
	if _, err := tmp.WriteString(updated); err != nil {
		tmp.Close()
		os.Remove(tmpPath)
		return false, fmt.Errorf("write tmp: %w", err)
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpPath)
		return false, fmt.Errorf("close tmp: %w", err)
	}
	if err := os.Rename(tmpPath, soulPath); err != nil {
		os.Remove(tmpPath)
		return false, fmt.Errorf("rename: %w", err)
	}
	slog.Info("soul skill-priority block injected", "component", "hermes", "path", soulPath)
	return true, nil
}

// upsertSoulSkillPriorityBlock returns soul with exactly one current copy of the
// skill-priority block at the end: any previous marker block is stripped (so
// stale wording from an older os-server never lingers), then the embedded block
// is re-appended below the persona/identity content.
func upsertSoulSkillPriorityBlock(soul string) string {
	if strings.Contains(soul, soulOSMarker) {
		soul = stripSoulMarkedBlock(soul)
	}
	soul = strings.TrimRight(soul, " \t\r\n")
	if soul == "" {
		return soulSkillPriorityBlock + "\n"
	}
	return soul + "\n\n" + soulSkillPriorityBlock + "\n"
}

// stripSoulMarkedBlock removes the block between soulOSMarker and the next ---
// separator line. Copied from runtimes/openclaw/onboarding.go stripMarkedBlock
// (package-private there).
func stripSoulMarkedBlock(text string) string {
	lines := strings.Split(text, "\n")
	var cleaned []string
	skip := false
	for _, line := range lines {
		trimmed := strings.TrimSpace(line)
		if trimmed == soulOSMarker {
			skip = true
			continue
		}
		if skip && trimmed == "---" {
			skip = false
			continue
		}
		if skip {
			continue
		}
		cleaned = append(cleaned, line)
	}
	return strings.Join(cleaned, "\n")
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
