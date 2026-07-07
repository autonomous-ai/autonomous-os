package claudecode

import (
	"context"
	"crypto/sha256"
	"embed"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/internal/device"
)

// knowledgeFS holds the KNOWLEDGE.md skeleton, embedded so a fresh
// claudecode-only device still gets the living-learnings doc the CLAUDE.md block
// imports. Identical template to internal/openclaw/resources/KNOWLEDGE.md.
//
//go:embed resources/KNOWLEDGE.md
var knowledgeFS embed.FS

// Onboarding (Claude Code). Mirrors internal/picoclaw/onboarding.go, trimmed to
// what the Claude Code backend owns on-device:
//
//   - The Claude Code CLI + bun + telegram channel plugin are installed
//     out-of-process by internal/claudecode/install.sh; the bridge + .env +
//     channel config are (re)materialized by presync.sh. Those run during the
//     switch-runtime flow AND from EnsureOnboarding below (hermes-style), so the
//     config self-heals on every boot/config change, not only on a switch.
//   - This file owns the OS-managed workspace markdown: CLAUDE.md (Claude Code's
//     auto-loaded memory file — holds the OS block with the persona @imports +
//     prompt discipline) and SOUL.md (per-device-type persona core block). The
//     persona satellite files (IDENTITY.md / USER.md / MEMORY.md / memory/) are
//     seeded empty so the @imports resolve; their content is owned by the agent
//     and the persona migration.
//   - There is NO HEARTBEAT.md: Claude Code has no heartbeat loop that would
//     read it (a conscious skip, not an oversight — see
//     docs/agentic/claudecode.md).
//
// When any OS-managed block changes, the bridge is restarted — CLAUDE.md and its
// imports are loaded at Claude session start only.

const (
	// osMandatoryMarker delimits the OS-managed block so it can be stripped +
	// re-injected cleanly on update. MUST match the marker used in the blocks below.
	osMandatoryMarker = "<!-- OS DO NOT REMOVE -->"

	// claudecodeWorkspaceDir is the cwd the bridge runs Claude Code in. Claude
	// auto-loads <cwd>/CLAUDE.md, <cwd>/.claude/skills, <cwd>/.mcp.json and
	// <cwd>/.claude/settings.json from here.
	claudecodeWorkspaceDir = claudecodeHome + "/workspace"

	// claudeMDBlock is the OS-managed block injected at the top of
	// workspace/CLAUDE.md. Two parts: (a) the persona @imports — CLAUDE.md is the
	// only file Claude Code loads by name, so SOUL/IDENTITY/USER/MEMORY/KNOWLEDGE
	// reach the context through these import lines; (b) the backend-agnostic
	// prompt discipline, derived from internal/picoclaw/onboarding.go
	// agentsMDBlock with the paths/commands adjusted to Claude Code (native
	// .claude/skills, `claude --version`).
	claudeMDBlock = `<!-- OS DO NOT REMOVE -->
**Persona & memory (OS-managed imports):** the following files hold who you are and what you remember — they are part of your context on every session:

@SOUL.md
@IDENTITY.md
@USER.md
@MEMORY.md
@KNOWLEDGE.md

**MANDATORY (skills):** Your device skills are native Claude Code skills in ` + "`.claude/skills/<name>/`" + `. For ordinary chat, simple Q&A, or meta discussion with no action/event/hardware behavior, do NOT invoke a skill — answer normally.
  - If the message contains ` + "`[skills: a, b, c]`" + `, treat it as an authoritative whitelist — use ONLY those skills. Do NOT scan other skills "just in case".
  - If no ` + "`[skills:]`" + ` hint is present and the user asks for a concrete action, hardware behavior, sensing/activity/emotion handling, or a specialized workflow, choose the single most specific matching skill.
  - If multiple skills plausibly match, choose the most specific one. If none clearly match, answer normally without a skill.

**Version check:** ` + "`os-server --version`" + ` (OS), ` + "`claude --version`" + ` (Claude Code), ` + "`curl -s http://127.0.0.1:5001/version`" + ` (HAL).

**Priority: Skills > Knowledge > memory/*.md > History.** A skill beats EVERYTHING (KNOWLEDGE.md, memory/*.md decisions, history). If memory says NO_REPLY but the skill says nudge, follow the skill. KNOWLEDGE.md is your personal observations — it can be wrong. Skills are the source of truth maintained by the developer. If you notice a conflict, update KNOWLEDGE.md to match the skill, not the other way around.

**Memory:** After each turn on any channel (voice, Telegram, or others) that contains something worth remembering (decisions, bugs, insights, new preferences), write it immediately to ` + "`memory/YYYY-MM-DD.md`" + `. Do not wait — context may be dropped before then.

**Memory writes — DESCRIBE, never PRESCRIBE.** Before writing any "decision/rule" to memory/*.md or KNOWLEDGE.md, re-check the relevant skill. Blanket forms like "X → always Y" / "X → NO_REPLY for all" are frequency disguised as rule — write what happened with conditions, not a blanket ban.

**Don't duplicate JSONL.** Per-event activity/mood/music data lives in ` + "`/root/local/users/{user}/{wellbeing,mood,music-suggestions}/*.jsonl`" + ` and ` + "`/root/local/flow_events_*.jsonl`" + `. If ` + "`cat`" + ` of a JSONL can answer it, DO NOT write to memory. Memory is for cross-day insights only.

**Mood awareness (MANDATORY): Follow the Mood skill.**

**User priority (MANDATORY):** When the turn batches multiple messages, ` + "`[user] ...`" + ` messages are direct human input (voice command or typed chat). Always answer the most recent ` + "`[user]`" + ` message first; treat ` + "`[activity]`" + ` / ` + "`[emotion]`" + ` / ` + "`[speech_emotion]`" + ` / ` + "`[ambient]`" + ` / ` + "`[sensing:*]`" + ` as supporting context, never as the primary prompt. A user who asked a question must get their answer even when sensing events queued alongside look more interesting.

---`
)

// SetupAgent materializes the device config by running the same presync
// EnsureOnboarding runs. The device setup flow calls this AFTER it persists
// config.json, so presync picks up the freshly-entered llm_* / channel tokens
// right away. The SetupRequest is unused — config.json (just saved) is the
// source of truth presync reads.
func (s *ClaudeCodeService) SetupAgent(_ domain.SetupRequest) error {
	return s.EnsureOnboarding()
}

// presyncStateFiles are the files presync owns whose content decides whether the
// bridge must restart after a presync run: the launch env (ANTHROPIC_* + channel
// flags) and the channel config. All are loaded at bridge/Claude start only
// (the bridge itself ships inside the os-server binary — nothing to hash).
var presyncStateFiles = []string{
	claudecodeHome + "/.env",
	"/root/.claude/channels/telegram/.env",
	"/root/.claude/channels/telegram/access.json",
	"/root/.claude/channels/discord/.env",
	"/root/.claude/channels/discord/access.json",
}

// EnsureOnboarding reconciles the device-side Claude Code state on every
// os-server boot / config change (server/config_watch.go — same path
// openclaw/hermes use):
//
//  1. re-run the embedded presync hook (.env + channel sync — the
//     SAME script switch-runtime runs before the backend starts), hermes-style,
//     so a device that boots straight into claudecode or changes llm_*/telegram
//     while active self-heals without a runtime switch;
//  2. seed KNOWLEDGE.md + the empty persona satellite files the CLAUDE.md
//     imports point at;
//  3. capability-gate skills + restore them from the CDN when the dir is empty
//     (factory reset / first boot);
//  4. refresh the OS-managed CLAUDE.md / SOUL.md blocks;
//  5. self-heal the systemd unit and restart the bridge only when something
//     actually changed (or it is down) — an unchanged boot is a no-op.
func (s *ClaudeCodeService) EnsureOnboarding() error {
	before := hashFiles(presyncStateFiles)
	if err := s.runPresync(); err != nil {
		// Best-effort: a presync failure must not block the bridge start below.
		slog.Warn("claudecode presync failed, continuing", "component", "claudecode", "error", err)
	}
	presyncChanged := hashFiles(presyncStateFiles) != before

	// Seed KNOWLEDGE.md from the embedded template only if absent (living doc —
	// never overwrite), then make sure every CLAUDE.md @import target exists so
	// a fresh workspace has no dangling imports.
	seedFileIfAbsent(knowledgeFS, "resources/KNOWLEDGE.md",
		filepath.Join(claudecodeWorkspaceDir, "KNOWLEDGE.md"))
	personaSeeded := ensurePersonaFiles()

	// Capability-gate skills; restore the supported set from the CDN when the
	// skills dir is empty (factory reset wiped it / first boot).
	s.pruneUnsupportedSkills()
	skillsRestored := s.ensureSkills()

	needRestart := presyncChanged || personaSeeded || skillsRestored

	if modified, err := s.ensureSoulMDBlock(); err != nil {
		slog.Error("ensure SOUL.md block failed", "component", "claudecode-onboarding", "error", err)
	} else if modified {
		needRestart = true
	}

	if modified, err := s.ensureClaudeMDBlock(); err != nil {
		slog.Error("ensure CLAUDE.md block failed", "component", "claudecode-onboarding", "error", err)
	} else if modified {
		needRestart = true
	}

	// Self-heal the unit (a device that reached claudecode without switch-runtime
	// has no unit) and (re)start when needed — mirrors hermes.EnsureOnboarding.
	unitInstalled := s.ensureGatewayUnit()
	unitDown := !gatewayActive()

	if !needRestart && !unitInstalled && !unitDown {
		slog.Info("claudecode onboarding: config + workspace unchanged, bridge up — no restart", "component", "claudecode")
		return nil
	}

	slog.Info("claudecode onboarding: (re)starting bridge", "component", "claudecode",
		"presync_changed", presyncChanged, "persona_seeded", personaSeeded,
		"skills_restored", skillsRestored, "unit_installed", unitInstalled, "unit_down", unitDown)
	enableClaudeCodeGateway()
	if err := restartClaudeCodeGateway(); err != nil {
		// Non-fatal: the new config is on disk; the bridge picks it up on its
		// next (re)start. Don't block the os-server boot path.
		slog.Warn("claudecode bridge restart failed", "component", "claudecode", "error", err)
	}
	return nil
}

// runPresync materializes the embedded presync script to a temp file and runs
// it. The script is self-contained (hardcodes /root/.claudecode +
// /root/config/config.json) and idempotent, so it is safe to run on every boot.
func (s *ClaudeCodeService) runPresync() error {
	f, err := os.CreateTemp("", "claudecode-presync-*.sh")
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

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	out, err := exec.CommandContext(ctx, "bash", path).CombinedOutput()
	if len(out) > 0 {
		slog.Info("claudecode presync output", "component", "claudecode", "output", strings.TrimSpace(string(out)))
	}
	if err != nil {
		return fmt.Errorf("run presync: %w", err)
	}
	return nil
}

// hashFiles returns a combined content hash of paths; absent files hash as "" so
// a file presync just created reads as changed.
func hashFiles(paths []string) string {
	var sb strings.Builder
	for _, p := range paths {
		b, err := os.ReadFile(p)
		if err != nil {
			sb.WriteString("|")
			continue
		}
		sum := sha256.Sum256(b)
		sb.Write(sum[:])
	}
	return sb.String()
}

// ensurePersonaFiles creates the empty persona satellite files the CLAUDE.md
// imports point at (IDENTITY.md / USER.md / MEMORY.md + the memory/ daily dir)
// so a fresh workspace has no dangling @imports. Never touches existing files —
// their content belongs to the agent / persona migration. Returns true when
// anything was created.
func ensurePersonaFiles() bool {
	created := false
	if err := os.MkdirAll(filepath.Join(claudecodeWorkspaceDir, "memory"), 0o755); err != nil {
		slog.Warn("ensure memory dir failed", "component", "claudecode-onboarding", "error", err)
	}
	for _, name := range []string{"IDENTITY.md", "USER.md", "MEMORY.md"} {
		path := filepath.Join(claudecodeWorkspaceDir, name)
		if _, err := os.Stat(path); err == nil {
			continue
		}
		if err := os.WriteFile(path, []byte(""), 0o644); err != nil {
			slog.Warn("seed persona file failed", "component", "claudecode-onboarding", "file", name, "error", err)
			continue
		}
		created = true
	}
	return created
}

// ensureClaudeMDBlock injects/refreshes the OS-managed block at the top of
// workspace/CLAUDE.md. Unlike picoclaw's AGENTS.md (generated by the runtime,
// skip-if-missing), CLAUDE.md is fully OS-owned here: created when absent, with
// an owner-editable section preserved below the block. Returns true if modified.
func (s *ClaudeCodeService) ensureClaudeMDBlock() (bool, error) {
	claudeFile := filepath.Join(claudecodeWorkspaceDir, "CLAUDE.md")

	content, err := os.ReadFile(claudeFile)
	if err != nil && !os.IsNotExist(err) {
		return false, fmt.Errorf("read CLAUDE.md: %w", err)
	}
	text := string(content)

	// Already has the exact current block → nothing to do.
	if strings.Contains(text, claudeMDBlock) {
		return false, nil
	}

	// Remove a stale marked block before injecting the current version.
	if strings.Contains(text, osMandatoryMarker) {
		text = stripMarkedBlock(text)
	}

	var output string
	if strings.TrimSpace(text) == "" {
		output = claudeMDBlock + "\n\n## Notes\n\n_Owner-editable. Add device-specific notes or personality tweaks here. The block above is managed by the OS and refreshed on each update — keep your edits in this section._\n"
	} else {
		output = claudeMDBlock + "\n\n" + strings.TrimLeft(text, " \t\r\n")
	}

	if err := os.MkdirAll(claudecodeWorkspaceDir, 0o755); err != nil {
		return false, fmt.Errorf("mkdir workspace: %w", err)
	}
	if err := os.WriteFile(claudeFile, []byte(output), 0o644); err != nil {
		return false, fmt.Errorf("write CLAUDE.md: %w", err)
	}
	slog.Info("injected mandatory block into CLAUDE.md", "component", "claudecode-onboarding", "path", claudeFile)
	return true, nil
}

// ensureSoulMDBlock wraps this device's soul as a marker-delimited core block at
// the top of workspace/SOUL.md; owner content below the closing `---` is
// preserved. Mirrors picoclaw/openclaw's ensureSoulMDBlock. The soul is resolved
// per device_type from DEVICE.md `soul_ref` (path or URL). A device that
// declares no soul injects nothing.
func (s *ClaudeCodeService) ensureSoulMDBlock() (bool, error) {
	soulFile := filepath.Join(claudecodeWorkspaceDir, "SOUL.md")

	coreContent, hasSoul, err := s.deviceSoulCore()
	if err != nil {
		return false, fmt.Errorf("resolve device soul: %w", err)
	}
	if !hasSoul {
		slog.Info("no soul_ref for device — leaving the migrated/default soul (no override)",
			"component", "claudecode-onboarding", "device_type", s.config.DeviceTypeOrDefault())
		return false, nil
	}
	soulMDBlock := osMandatoryMarker + "\n" + strings.TrimSpace(string(coreContent)) + "\n---"

	content, err := os.ReadFile(soulFile)
	if err != nil && !os.IsNotExist(err) {
		return false, fmt.Errorf("read SOUL.md: %w", err)
	}
	text := string(content)

	// Fast path: block already present with only owner content below it.
	if idx := strings.Index(text, soulMDBlock); idx >= 0 {
		below := strings.TrimLeft(text[idx+len(soulMDBlock):], " \t\r\n")
		if !isDefaultSoulHeading(below) {
			return false, nil
		}
	}

	if strings.Contains(text, osMandatoryMarker) {
		text = stripMarkedBlock(text)
	}

	// Discard a managed default soul left in the remaining text so it is not
	// preserved as fake owner content below the device block.
	trimmed := strings.TrimLeft(text, " \t\r\n")
	if isDefaultSoulHeading(trimmed) {
		if idx := strings.Index(text, "## Personal"); idx >= 0 {
			text = text[idx:]
		} else {
			text = ""
		}
	}

	var output string
	if strings.TrimSpace(text) == "" {
		output = soulMDBlock + "\n\n## Personal\n\n_Owner-editable. Add notes about yourself, family, routines, or personality tweaks here. The block above is managed by the OS and will be refreshed on each update — keep your edits in this section._\n"
	} else {
		output = soulMDBlock + "\n\n" + text
	}

	if output == string(content) {
		return false, nil
	}
	if err := os.MkdirAll(claudecodeWorkspaceDir, 0o755); err != nil {
		return false, fmt.Errorf("mkdir workspace: %w", err)
	}
	if err := os.WriteFile(soulFile, []byte(output), 0o644); err != nil {
		return false, fmt.Errorf("write SOUL.md: %w", err)
	}
	slog.Info("injected core block into SOUL.md", "component", "claudecode-onboarding", "path", soulFile)
	return true, nil
}

// deviceSoulCore resolves the soul text for this device from the `soul_ref` in
// devices/<type>/DEVICE.md. Mirrors openclaw/picoclaw's deviceSoulCore.
func (s *ClaudeCodeService) deviceSoulCore() (content []byte, hasSoul bool, err error) {
	devType := s.config.DeviceTypeOrDefault()
	ref := device.SoulRef(devType)
	if ref == "" {
		return nil, false, nil // soulless body: no override
	}
	if strings.HasPrefix(ref, "http://") || strings.HasPrefix(ref, "https://") {
		b, derr := downloadSoul(ref)
		if derr != nil {
			return nil, false, fmt.Errorf("download soul_ref %q: %w", ref, derr)
		}
		return b, true, nil
	}
	if strings.Contains(ref, "://") {
		return nil, false, fmt.Errorf("unsupported soul_ref scheme: %q (use http(s):// or a path)", ref)
	}
	path := filepath.Join(device.DevicesDir(), devType, ref)
	b, rerr := os.ReadFile(path)
	if rerr != nil {
		return nil, false, fmt.Errorf("read soul_ref %q: %w", path, rerr)
	}
	return b, true, nil
}

// downloadSoul fetches a soul artifact named by an http(s) soul_ref.
func downloadSoul(url string) ([]byte, error) {
	client := &http.Client{Timeout: 30 * time.Second}
	resp, err := client.Get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("status %d", resp.StatusCode)
	}
	return io.ReadAll(resp.Body)
}

// isDefaultSoulHeading reports whether trimmed begins with a managed soul
// template heading that must not be preserved as owner content.
func isDefaultSoulHeading(trimmed string) bool {
	return strings.HasPrefix(trimmed, "# Soul") || strings.HasPrefix(trimmed, "# SOUL.md")
}

// claudecodeBuiltinSkills — Claude Code ships no device-local bundled skills of
// its own (plugin skills live inside the plugin dirs, not the workspace), so
// only the capability-gated platform skills exist under .claude/skills and the
// keep-list is empty. Kept as a map for symmetry with picoclawBuiltinSkills.
var claudecodeBuiltinSkills = map[string]bool{}

// pruneUnsupportedSkills removes skill dirs the device can't use from
// workspace/.claude/skills. A skill survives when it is EITHER (a) supported by
// this device's capabilities (skills.Supported — the same gate openclaw uses) OR
// (b) a claudecode built-in. Fail-open: when DEVICE.md declares no capabilities,
// skills.Supported returns the full catalog, so nothing is pruned.
func (s *ClaudeCodeService) pruneUnsupportedSkills() {
	skillsDir := filepath.Join(claudecodeWorkspaceDir, ".claude", "skills")
	entries, err := os.ReadDir(skillsDir)
	if err != nil {
		if !os.IsNotExist(err) {
			slog.Warn("prune skills: read dir failed", "component", "claudecode-onboarding", "error", err)
		}
		return
	}
	keep := map[string]bool{}
	for _, n := range s.supportedSkills() {
		keep[n] = true
	}
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		name := e.Name()
		if keep[name] || claudecodeBuiltinSkills[name] {
			continue
		}
		if err := os.RemoveAll(filepath.Join(skillsDir, name)); err != nil {
			slog.Warn("prune unsupported skill failed", "component", "claudecode-onboarding", "skill", name, "error", err)
			continue
		}
		slog.Info("pruned unsupported skill (capability gate)", "component", "claudecode-onboarding", "skill", name)
	}
}

// ensureSkills downloads all supported skills from the CDN into
// workspace/.claude/skills when the directory is absent or empty. Covers factory
// reset (which wipes the workspace) and first boot. Steady-state updates are
// handled by StartSkillWatcher (version-gated CDN polling). Returns true when
// skills were restored so EnsureOnboarding includes a bridge restart. Mirrors
// hermes.ensureSkills.
func (s *ClaudeCodeService) ensureSkills() bool {
	skillsDir := filepath.Join(claudecodeWorkspaceDir, ".claude", "skills")
	entries, err := os.ReadDir(skillsDir)
	if err == nil && len(entries) > 0 {
		return false // skills present — watcher handles updates
	}
	names := s.supportedSkills()
	if len(names) == 0 {
		return false
	}
	slog.Info("claudecode onboarding: skills dir empty — restoring from CDN",
		"component", "claudecode", "count", len(names))
	changed := s.downloadSkillsByName(names)
	slog.Info("claudecode onboarding: skills restored", "component", "claudecode", "restored", len(changed))
	return len(changed) > 0
}

// seedFileIfAbsent writes an embedded file to dst only when dst does not already
// exist (never overwrites — KNOWLEDGE.md is a living doc).
func seedFileIfAbsent(efs embed.FS, src, dst string) {
	if _, err := os.Stat(dst); err == nil {
		return // already exists, never overwrite
	}
	data, err := efs.ReadFile(src)
	if err != nil {
		slog.Error("read embedded file failed", "component", "claudecode-onboarding", "src", src, "error", err)
		return
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0755); err != nil {
		slog.Error("create dir for seed failed", "component", "claudecode-onboarding", "dst", dst, "error", err)
		return
	}
	if err := os.WriteFile(dst, data, 0644); err != nil {
		slog.Error("write file failed", "component", "claudecode-onboarding", "dst", dst, "error", err)
		return
	}
	slog.Info("seeded file (initial)", "component", "claudecode-onboarding", "file", filepath.Base(dst))
}

// stripMarkedBlock removes the block between the marker and the next ---
// separator. Copied from internal/openclaw/onboarding.go (package-private there).
func stripMarkedBlock(text string) string {
	lines := strings.Split(text, "\n")
	var cleaned []string
	skip := false
	for _, line := range lines {
		trimmed := strings.TrimSpace(line)
		if trimmed == osMandatoryMarker {
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
