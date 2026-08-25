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

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
)

// knowledgeFS holds the KNOWLEDGE.md skeleton, embedded so a fresh
// claudecode-only device still gets the living-learnings doc the CLAUDE.md block
// imports. Identical template to runtimes/openclaw/resources/KNOWLEDGE.md.
//
//go:embed resources/KNOWLEDGE.md
var knowledgeFS embed.FS

// Onboarding (Claude Code). Mirrors runtimes/picoclaw/onboarding.go, trimmed to
// what the Claude Code backend owns on-device:
//
//   - The Claude Code CLI is installed out-of-process by
//     runtimes/claudecode/install.sh (no bun/channel plugins — telegram +
//     discord are device-owned loops); the bridge + .env are (re)materialized
//     by presync.sh. Those run during the
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
	// auto-loads <cwd>/CLAUDE.md, <cwd>/.mcp.json and <cwd>/.claude/settings.json
	// from here. Skills are NOT installed here — see claudecodeSkillsDir.
	claudecodeWorkspaceDir = claudecodeHome + "/workspace"

	// claudeUserDir is Claude Code's user-level config dir. The gatewayd runs the
	// claude child with HOME=/root (gatewayd.Config.Home), so this is $HOME/.claude.
	claudeUserDir = "/root/.claude"

	// claudeUserMDPath is the user-level memory file. Claude Code loads it in EVERY
	// session regardless of cwd — unlike the workspace CLAUDE.md, which only the
	// device-chat session (cwd = workspace) ever sees. The OS block lives here so
	// persona + rules follow the agent into coding sessions.
	claudeUserMDPath = claudeUserDir + "/CLAUDE.md"

	// claudecodeSkillsDir is where device skills are installed: USER-scoped, not
	// project-scoped. Claude Code resolves PROJECT skills as <cwd>/.claude/skills,
	// so a workspace-only install is invisible to any session whose cwd is not the
	// workspace — notably the coding sessions the device spawns in /root, /root/myapp,
	// … (coding_sessions.go). Those sessions then can't see the connectors skill and
	// tell the user their Gmail/Calendar is "not connected" (or write their own
	// send_email.py) while the credentials sit on disk. USER-level skills load in
	// EVERY session regardless of cwd, so the device chat AND coding sessions both
	// get them.
	claudecodeSkillsDir = claudeUserDir + "/skills"

	// claudeMDBlock is the OS-managed block injected at the top of the USER-level
	// memory file (~/.claude/CLAUDE.md — claudeUserMDPath), NOT the workspace one.
	// Claude Code loads the user file in every session regardless of cwd, so the
	// device's persona + rules follow the agent into the coding sessions it spawns
	// in other folders (/root, /root/myapp, …); a workspace-scoped block only ever
	// reached the device-chat session.
	//
	// Two parts: (a) the persona @imports — CLAUDE.md is the only file Claude Code
	// loads by name, so SOUL/IDENTITY/USER/MEMORY/KNOWLEDGE reach the context
	// through these import lines; (b) the backend-agnostic prompt discipline,
	// derived from runtimes/picoclaw/onboarding.go agentsMDBlock with the
	// paths/commands adjusted to Claude Code (native skills, `claude --version`).
	//
	// EVERY path here MUST be absolute. The block is read from sessions whose cwd
	// is an arbitrary folder, so a relative `@SOUL.md` would resolve against that
	// folder (persona silently missing) and a relative `memory/…` would scatter
	// memory files into whatever project the user is coding in.
	claudeMDBlock = `<!-- OS DO NOT REMOVE -->
**Persona & memory (OS-managed imports):** the following files hold who you are and what you remember — they are part of your context on every session:

@/root/.claudecode/workspace/SOUL.md
@/root/.claudecode/workspace/IDENTITY.md
@/root/.claudecode/workspace/USER.md
@/root/.claudecode/workspace/MEMORY.md
@/root/.claudecode/workspace/KNOWLEDGE.md

**MANDATORY (skills):** Your device skills are native Claude Code skills in ` + "`~/.claude/skills/<name>/`" + ` (user-scoped — available in every session, including coding sessions in any folder). For ordinary chat or meta discussion with no action/hardware behavior and no connected-service data, do NOT invoke a skill — answer normally. A question ABOUT a linked third-party service (see Connectors) is NOT ordinary chat: it needs the skill even when phrased as a simple question.
  - If the message contains ` + "`[skills: a, b, c]`" + `, treat it as an authoritative whitelist — use ONLY those skills. Do NOT scan other skills "just in case".
  - If no ` + "`[skills:]`" + ` hint is present and the user asks for a concrete action, hardware behavior, sensing/activity/emotion handling, data from a linked service, or a specialized workflow, choose the single most specific matching skill.
  - If multiple skills plausibly match, choose the most specific one. If none clearly match, answer normally without a skill.

**Connectors (MANDATORY):** The user links third-party services (Gmail, Google Calendar, Google Drive, Notion, Figma, Asana, Linear, GitHub, Ahrefs, …) in the app, and the OS writes their credentials to this device at ` + "`/root/.openclaw/workspace/configs/<code>_access_tokens.json`" + `. ALWAYS use the ` + "`connectors`" + ` skill to answer or act on ANY of them — including "is my gmail connected?", "what's on my calendar", "list my events", "check my email". The skill lives at ` + "`/root/.claude/skills/connectors/SKILL.md`" + `: invoke it with the Skill tool, and if for any reason it is not offered to you, READ that file and follow it. This holds in EVERY session — including a coding session started in ` + "`/root`" + `, ` + "`/root/myapp`" + `, or any other folder: the connectors are a property of the DEVICE, not of the folder you happen to be in. Never write your own script (` + "`send_email.py`" + `, gcalcli, …) to reach a service a connector already covers.
  - ` + "`.mcp.json`" + ` is NOT the connector list. Gmail/Calendar/Drive are token-based and have NO MCP server, so NEVER conclude a service is unconnected because it is missing from the MCP server list — check the credential files via the skill.
  - Never tell the user to set up an MCP server, OAuth app, or another CLI for these services: the credentials are already on disk. Read them only through the ` + "`connectors`" + ` skill, which knows how to keep the secrets safe.

**Version check:** ` + "`os-server --version`" + ` (OS), ` + "`claude --version`" + ` (Claude Code), ` + "`curl -s http://127.0.0.1:5001/version`" + ` (HAL).

**Priority: Skills > Knowledge > memory/*.md > History.** A skill beats EVERYTHING (KNOWLEDGE.md, memory/*.md decisions, history). If memory says NO_REPLY but the skill says nudge, follow the skill. KNOWLEDGE.md is your personal observations — it can be wrong. Skills are the source of truth maintained by the developer. If you notice a conflict, update KNOWLEDGE.md to match the skill, not the other way around.

**Memory:** After each turn on any channel (voice, Telegram, or others) that contains something worth remembering (decisions, bugs, insights, new preferences), write it immediately to ` + "`/root/.claudecode/workspace/memory/YYYY-MM-DD.md`" + `. Do not wait — context may be dropped before then. Always use that ABSOLUTE path: your memory belongs to the device, not to whatever folder you are working in — never create a ` + "`memory/`" + ` dir inside a project you happen to be coding in.

**Silence = the literal token NO_REPLY.** When a skill says not to speak, output exactly NO_REPLY and nothing else. Never narrate the decision ("Sound event, no user message. Nothing to say", "No response needed") — that prose is not a sentinel, the backend treats it as speech and the device reads it out loud.

**Memory writes — DESCRIBE, never PRESCRIBE.** Before writing any "decision/rule" to memory/*.md or KNOWLEDGE.md, re-check the relevant skill. Blanket forms like "X → always Y" / "X → NO_REPLY for all" are frequency disguised as rule — write what happened with conditions, not a blanket ban.

**Don't duplicate JSONL.** Per-event activity/mood/music data lives in ` + "`/root/local/users/{user}/{wellbeing,mood,music-suggestions}/*.jsonl`" + ` and ` + "`/root/local/flow_events_*.jsonl`" + `. If ` + "`cat`" + ` of a JSONL can answer it, DO NOT write to memory. Memory is for cross-day insights only. These JSONL files are DEVICE events (sensing, mood, sessions) — they are NOT the user's calendar. "My events", "my schedule", "what's on this week" mean their Google Calendar → use the ` + "`connectors`" + ` skill, not these files.

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
//  3. capability-gate and reconcile every supported skill from the CDN;
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

	// Lift any project-scoped skills left by an older os-server to user scope
	// FIRST, so the prune/restore below see the real (post-migration) dir.
	skillsMigrated := migrateSkillsToUserScope()

	// Capability-gate skills, then reconcile the entire supported set from the
	// CDN. This also repairs an old local skill when the watcher starts after OTA
	// metadata was already published at its new version.
	s.pruneUnsupportedSkills()
	changedSkills := s.downloadSkills()
	skillsSynced := len(changedSkills) > 0

	needRestart := presyncChanged || personaSeeded || skillsSynced || skillsMigrated

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

	// The OS block now lives in the user-level file (ensureClaudeMDBlock above);
	// strip the legacy workspace copy so the device chat doesn't load it twice.
	if dropWorkspaceClaudeMDBlock() {
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
		"skills_synced", skillsSynced, "unit_installed", unitInstalled, "unit_down", unitDown)
	enableClaudeCodeGateway()
	if err := restartClaudeCodeGateway(); err != nil {
		// Non-fatal: the new config is on disk; the bridge picks it up on its
		// next (re)start. Don't block the os-server boot path.
		slog.Warn("claudecode bridge restart failed", "component", "claudecode", "error", err)
	}
	// Send this after the restart attempt so the bridge is available to receive
	// the re-read instruction, matching OpenClaw's onboarding ordering.
	s.notifySkillChanges(changedSkills)
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

// ensureClaudeMDBlock injects/refreshes the OS-managed block at the top of the
// USER-level ~/.claude/CLAUDE.md, which Claude Code loads in every session
// whatever the cwd — so the device persona + rules reach the coding sessions too.
// The file is OS-owned: created when absent, with an owner-editable section
// preserved below the block. Returns true if modified.
func (s *ClaudeCodeService) ensureClaudeMDBlock() (bool, error) {
	claudeFile := claudeUserMDPath

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

	if err := os.MkdirAll(claudeUserDir, 0o755); err != nil {
		return false, fmt.Errorf("mkdir %s: %w", claudeUserDir, err)
	}
	if err := os.WriteFile(claudeFile, []byte(output), 0o644); err != nil {
		return false, fmt.Errorf("write CLAUDE.md: %w", err)
	}
	slog.Info("injected mandatory block into user CLAUDE.md", "component", "claudecode-onboarding", "path", claudeFile)
	return true, nil
}

// dropWorkspaceClaudeMDBlock strips the OS block from the legacy workspace
// CLAUDE.md now that it lives at user scope. Both files load in the device-chat
// session (project + user memory), so leaving the old copy would duplicate the
// whole block in context — and its RELATIVE @imports/memory paths are exactly
// what the move to absolute paths fixed. Owner notes below the block survive; the
// file is deleted only when nothing but the block was in it. Returns true when
// modified.
func dropWorkspaceClaudeMDBlock() bool {
	path := filepath.Join(claudecodeWorkspaceDir, "CLAUDE.md")
	content, err := os.ReadFile(path)
	if err != nil {
		return false // absent — already migrated / fresh device
	}
	text := string(content)
	if !strings.Contains(text, osMandatoryMarker) {
		return false // no OS block to strip (pure owner notes)
	}

	rest := strings.TrimSpace(stripMarkedBlock(text))
	if rest == "" {
		if err := os.Remove(path); err != nil {
			slog.Warn("drop workspace CLAUDE.md failed", "component", "claudecode-onboarding", "error", err)
			return false
		}
		slog.Info("removed workspace CLAUDE.md (OS block now user-scoped)", "component", "claudecode-onboarding", "path", path)
		return true
	}
	if err := os.WriteFile(path, []byte(rest+"\n"), 0o644); err != nil {
		slog.Warn("strip workspace CLAUDE.md block failed", "component", "claudecode-onboarding", "error", err)
		return false
	}
	slog.Info("stripped OS block from workspace CLAUDE.md (kept owner notes)", "component", "claudecode-onboarding", "path", path)
	return true
}

// ensureSoulMDBlock wraps this device's soul as a marker-delimited core block at
// the top of workspace/SOUL.md; owner content below the closing `---` is
// preserved. Mirrors picoclaw/openclaw's ensureSoulMDBlock. The soul is resolved
// per device_type from ROBOT.md `soul_ref` (path or URL). A device that
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
// robots/<type>/ROBOT.md. Mirrors openclaw/picoclaw's deviceSoulCore.
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
// claudecodeSkillsDir. A skill survives when it is EITHER (a) supported by
// this device's capabilities (skills.Supported — the same gate openclaw uses) OR
// (b) a claudecode built-in. Fail-open: when ROBOT.md declares no capabilities,
// skills.Supported returns the full catalog, so nothing is pruned.
func (s *ClaudeCodeService) pruneUnsupportedSkills() {
	skillsDir := claudecodeSkillsDir
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

// migrateSkillsToUserScope moves skills installed by an older os-server under
// workspace/.claude/skills into the user-level claudecodeSkillsDir, then drops
// the workspace copy. Without the move, devices already in the field would keep
// project-scoped-only skills (invisible to coding sessions); without the drop,
// Claude Code would register every skill twice (project + user) in the device
// chat. Idempotent: a no-op once the legacy dir is gone. Returns true when
// anything moved, so EnsureOnboarding restarts the bridge.
func migrateSkillsToUserScope() bool {
	legacyDir := filepath.Join(claudecodeWorkspaceDir, ".claude", "skills")
	entries, err := os.ReadDir(legacyDir)
	if err != nil {
		return false // absent (already migrated / fresh device) — nothing to do
	}

	if err := os.MkdirAll(claudecodeSkillsDir, 0o755); err != nil {
		slog.Warn("migrate skills: mkdir user skills dir failed", "component", "claudecode-onboarding", "error", err)
		return false
	}

	var moved int
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		dst := filepath.Join(claudecodeSkillsDir, e.Name())
		if _, err := os.Stat(dst); err == nil {
			continue // already at user scope — the legacy copy is redundant
		}
		if err := os.Rename(filepath.Join(legacyDir, e.Name()), dst); err != nil {
			slog.Warn("migrate skills: move failed", "component", "claudecode-onboarding", "skill", e.Name(), "error", err)
			continue
		}
		moved++
	}

	// Drop the legacy dir even when nothing moved (every skill already existed at
	// user scope) — leaving it would double-register each skill.
	if err := os.RemoveAll(legacyDir); err != nil {
		slog.Warn("migrate skills: remove legacy dir failed", "component", "claudecode-onboarding", "error", err)
	}
	slog.Info("migrated skills to user scope", "component", "claudecode-onboarding",
		"moved", moved, "from", legacyDir, "to", claudecodeSkillsDir)
	return true
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
// separator. Copied from runtimes/openclaw/onboarding.go (package-private there).
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
