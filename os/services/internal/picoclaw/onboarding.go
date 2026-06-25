package picoclaw

import (
	"embed"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"go.autonomous.ai/os/internal/device"
)

// knowledgeFS holds the KNOWLEDGE.md skeleton, embedded so a fresh picoclaw-only
// device (one that never ran openclaw, so presync.sh §0 had nothing to copy) still
// gets the living-learnings doc the AGENTS.md block tells the agent to read.
// Identical template to internal/openclaw/resources/KNOWLEDGE.md.
//
//go:embed resources/KNOWLEDGE.md
var knowledgeFS embed.FS

// Onboarding (PicoClaw). Mirrors internal/openclaw/onboarding.go, but trimmed to
// what PicoClaw actually owns on-device:
//
//   - The PicoClaw runtime is installed + provisioned out-of-process by
//     internal/picoclaw/install.sh + presync.sh (binary, `picoclaw onboard`,
//     `picoclaw migrate`, model/channel config). Those run during the
//     switch-runtime flow, NOT here.
//   - This file owns the OS-managed blocks in the workspace markdown — AGENTS.md
//     (prompt rules), SOUL.md (per-device-type persona), HEARTBEAT.md (daily
//     knowledge-synthesis) — plus the KNOWLEDGE.md seed, the same contract openclaw
//     has, so a plain os-server OTA keeps them current. PicoClaw reads AGENTS.md on
//     the legacy path (presync deletes AGENT.md so that path is active and
//     IDENTITY.md is read). When any block changes, the gateway is restarted.
//
// The block is OpenClaw-derived but stripped of OpenClaw-only bits (the
// hooks/handler.ts paragraph and `openclaw --version`); everything else is
// backend-agnostic prompt discipline (skills selection, memory rules, priority).

const (
	// osMandatoryMarker delimits the OS-managed block so it can be stripped +
	// re-injected cleanly on update. MUST match the marker used in the block below.
	osMandatoryMarker = "<!-- OS DO NOT REMOVE -->"

	// picoclawWorkspaceDir is PicoClaw's workspace (HOME=/root → ~/.picoclaw).
	// TODO(picoclaw-config-dir): there is no PicoclawConfigDir in server/config yet
	// (openclaw uses cfg.OpenclawConfigDir). Hardcoded for now; promote to config if
	// the data dir ever needs to be overridable.
	picoclawWorkspaceDir = "/root/.picoclaw/workspace"

	// agentsMDBlock is the OS-managed block injected into workspace/AGENTS.md.
	// Derived from internal/openclaw/onboarding.go agentsMDBlock with OpenClaw-only
	// content removed (hooks/handler.ts; `openclaw --version`; the injected
	// `<available_skills>` wording).
	agentsMDBlock = `<!-- OS DO NOT REMOVE -->
**MANDATORY (skills):** Before any skill-driven action, determine the skill scope without doing broad filesystem scans. For ordinary chat, simple Q&A, or meta discussion with no action/event/hardware behavior, do NOT read a SKILL.md — answer normally.
  - If the message contains ` + "`[skills: a, b, c]`" + `, treat it as an authoritative whitelist — read ONLY those ` + "`skills/<name>/SKILL.md`" + ` files. Do NOT scan other skill directories "just in case".
  - If no ` + "`[skills:]`" + ` hint is present and the user asks for a concrete action, hardware behavior, sensing/activity/emotion handling, or a specialized workflow, choose the single most specific matching skill, then read only that SKILL.md.
  - If multiple skills plausibly match, choose the most specific one. If none clearly match, do not read any SKILL.md and answer normally.
  - Never fall back to reading every skill directory. Broad scans are slow and usually reduce quality.

Follow the instructions in whichever file you read.

**Version check:** ` + "`os-server --version`" + ` (OS), ` + "`picoclaw --version`" + ` (PicoClaw), ` + "`curl -s http://127.0.0.1:5001/version`" + ` (HAL).

**Session Startup — also read:** ` + "`KNOWLEDGE.md`" + ` (accumulated learnings) in addition to the steps listed below.

**Priority: Skills > Knowledge > memory/*.md > History.** SKILL.md beats EVERYTHING (KNOWLEDGE.md, memory/*.md decisions, history). If memory says NO_REPLY but SKILL says nudge, follow SKILL. KNOWLEDGE.md is your personal observations — it can be wrong. Skills are the source of truth maintained by the developer. If you notice a conflict, update KNOWLEDGE.md to match the skill, not the other way around.

**Memory:** After each turn on any channel (voice, Telegram, or others) that contains something worth remembering (decisions, bugs, insights, new preferences), write it immediately to ` + "`memory/YYYY-MM-DD.md`" + `. Do not wait for heartbeat — context may be dropped before then.

**Memory writes — DESCRIBE, never PRESCRIBE.** Before writing any "decision/rule" to memory/*.md or KNOWLEDGE.md, re-read the relevant SKILL.md. Blanket forms like "X → always Y" / "X → NO_REPLY for all" are frequency disguised as rule — write what happened with conditions, not a blanket ban.

**Don't duplicate JSONL.** Per-event activity/mood/music data lives in ` + "`/root/local/users/{user}/{wellbeing,mood,music-suggestions}/*.jsonl`" + ` and ` + "`/root/local/flow_events_*.jsonl`" + `. If ` + "`cat`" + ` of a JSONL can answer it, DO NOT write to memory. Memory is for cross-day insights only.

**Mood awareness (MANDATORY): Follow Mood skill.**

**User priority (MANDATORY):** When the turn batches multiple messages, ` + "`[user] ...`" + ` messages are direct human input (voice command or typed chat). Always answer the most recent ` + "`[user]`" + ` message first; treat ` + "`[activity]`" + ` / ` + "`[emotion]`" + ` / ` + "`[speech_emotion]`" + ` / ` + "`[ambient]`" + ` / ` + "`[sensing:*]`" + ` as supporting context, never as the primary prompt. A user who asked a question must get their answer even when sensing events queued alongside look more interesting.

---`

	// heartbeatMDBlock is the OS-managed knowledge-synthesis block injected at the top
	// of workspace/HEARTBEAT.md. Backend-agnostic — verbatim from openclaw.
	heartbeatMDBlock = `<!-- OS DO NOT REMOVE -->
**Knowledge synthesis (once daily at 21:00):** If current time is >= 21:00 AND you have NOT already done this today (check ` + "`KNOWLEDGE.md`" + ` for today's date header), read today's ` + "`memory/YYYY-MM-DD.md`" + `, extract important insights, and append them to ` + "`KNOWLEDGE.md`" + ` under a ` + "`## YYYY-MM-DD`" + ` header. Only write new learnings — do not repeat what is already there. If already done today or before 21:00, skip silently.

---`
)

// EnsureOnboarding reconciles the device-side PicoClaw workspace. Called on boot +
// config changes via server/config_watch.go (same path openclaw/hermes use). Today
// it only keeps the OS-managed block in AGENTS.md current; the rest of PicoClaw
// provisioning is owned by install.sh/presync.sh (see file header).
func (s *PicoclawService) EnsureOnboarding() error {
	// Seed KNOWLEDGE.md from the embedded template only if absent. presync.sh §0
	// copies openclaw's living KNOWLEDGE.md (with accumulated learnings) when
	// migrating; this fallback covers the fresh picoclaw-only device where there was
	// no openclaw copy. Never overwrites an existing file.
	seedFileIfAbsent(knowledgeFS, "resources/KNOWLEDGE.md",
		filepath.Join(picoclawWorkspaceDir, "KNOWLEDGE.md"))

	// Capability-gate skills: drop platform skills this device can't use (e.g.
	// servo-control on a motionless device), keeping picoclaw's built-ins. Skill dirs
	// are read per-turn from disk, so no gateway reload is needed.
	s.pruneUnsupportedSkills()

	needRestart := false

	// SOUL.md per-device-type core block (owner-editable content stays below it).
	if modified, err := s.ensureSoulMDBlock(); err != nil {
		slog.Error("ensure SOUL.md block failed", "component", "picoclaw-onboarding", "error", err)
	} else if modified {
		needRestart = true
	}

	// AGENTS.md mandatory block (skills/memory/priority prompt rules).
	if modified, err := s.ensureAgentsMDBlock(); err != nil {
		slog.Error("ensure AGENTS.md block failed", "component", "picoclaw-onboarding", "error", err)
	} else if modified {
		needRestart = true
	}

	// HEARTBEAT.md knowledge-synthesis block.
	if modified, err := s.ensureHeartbeatMDBlock(); err != nil {
		slog.Error("ensure HEARTBEAT.md block failed", "component", "picoclaw-onboarding", "error", err)
	} else if modified {
		needRestart = true
	}

	// Restart the gateway so it re-reads the changed workspace prompt files
	// (systemctl restart — see service_gateway.go for why not /reload).
	if needRestart {
		slog.Info("restarting picoclaw gateway to pick up workspace changes", "component", "picoclaw-onboarding")
		if err := restartPicoclawGateway(); err != nil {
			return fmt.Errorf("restart picoclaw after onboarding: %w", err)
		}
	}

	// TODO(picoclaw-onboarding-parity): openclaw additionally pins messages.queue.mode
	// (picoclaw has its own steering_mode — verify before mirroring). (openclaw.json-
	// specific steps — hooks/logging/controlUi — are N/A for picoclaw's config.json;
	// skill capability-gating is done above via pruneUnsupportedSkills.)
	return nil
}

// ensureAgentsMDBlock injects/refreshes the OS-managed block in workspace/AGENTS.md.
// Returns true if the file was modified. Mirrors openclaw's ensureAgentsMDBlock.
func (s *PicoclawService) ensureAgentsMDBlock() (bool, error) {
	agentsFile := filepath.Join(picoclawWorkspaceDir, "AGENTS.md")

	if _, err := os.Stat(agentsFile); os.IsNotExist(err) {
		// TODO(picoclaw-agents-template): openclaw regenerates a base AGENTS.md via
		// `openclaw setup` when it is missing. PicoClaw's AGENTS.md comes from
		// `picoclaw migrate` (presync.sh §0) instead, and there is no equivalent
		// regenerate command, so skip injection rather than write to an empty file.
		slog.Warn("AGENTS.md missing — skipping block injection (no picoclaw regenerate)",
			"component", "picoclaw-onboarding", "path", agentsFile)
		return false, nil
	}

	content, err := os.ReadFile(agentsFile)
	if err != nil {
		return false, fmt.Errorf("read AGENTS.md: %w", err)
	}
	text := string(content)

	// Already has the exact current block → nothing to do.
	if strings.Contains(text, agentsMDBlock) {
		return false, nil
	}

	// Remove a stale marked block before injecting the current version.
	if strings.Contains(text, osMandatoryMarker) {
		text = stripMarkedBlock(text)
	}

	// Inject below the "Your workspace" line; prepend to the top if it isn't found.
	lines := strings.Split(text, "\n")
	result := make([]string, 0, len(lines)+2)
	injected := false
	for _, line := range lines {
		result = append(result, line)
		if !injected && strings.Contains(strings.ToLower(line), "your workspace") {
			result = append(result, agentsMDBlock)
			injected = true
		}
	}
	if !injected {
		result = append([]string{agentsMDBlock, ""}, result...)
	}

	if err := os.WriteFile(agentsFile, []byte(strings.Join(result, "\n")), 0644); err != nil {
		return false, fmt.Errorf("write AGENTS.md: %w", err)
	}
	slog.Info("injected mandatory block into AGENTS.md", "component", "picoclaw-onboarding", "path", agentsFile)
	return true, nil
}

// ensureSoulMDBlock wraps this device's soul as a marker-delimited core block at the
// top of workspace/SOUL.md; owner content below the closing `---` is preserved.
// Mirrors openclaw's ensureSoulMDBlock. The soul is resolved per device_type from
// DEVICE.md `soul_ref` (path or URL). A device that declares no soul injects nothing.
func (s *PicoclawService) ensureSoulMDBlock() (bool, error) {
	soulFile := filepath.Join(picoclawWorkspaceDir, "SOUL.md")

	coreContent, hasSoul, err := s.deviceSoulCore()
	if err != nil {
		return false, fmt.Errorf("resolve device soul: %w", err)
	}
	if !hasSoul {
		slog.Info("no soul_ref for device — leaving the migrated/default soul (no override)",
			"component", "picoclaw-onboarding", "device_type", s.config.DeviceTypeOrDefault())
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

	// Strip a prior marker block so the default-seed heuristic only sees what was
	// below the closing `---`.
	if strings.Contains(text, osMandatoryMarker) {
		text = stripMarkedBlock(text)
	}

	// Discard a managed default soul left in the remaining text so it is not preserved
	// as fake owner content and duplicated below the device block. Keep an owner-added
	// `## Personal` section if present.
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
	if err := os.WriteFile(soulFile, []byte(output), 0644); err != nil {
		return false, fmt.Errorf("write SOUL.md: %w", err)
	}
	slog.Info("injected core block into SOUL.md", "component", "picoclaw-onboarding", "path", soulFile)
	return true, nil
}

// deviceSoulCore resolves the soul text for this device from the `soul_ref` in
// devices/<type>/DEVICE.md. Mirrors openclaw's deviceSoulCore: absent → no override;
// http(s) → download; otherwise a path relative to devices/<type>/.
func (s *PicoclawService) deviceSoulCore() (content []byte, hasSoul bool, err error) {
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
	path := filepath.Join(devicesDir(), devType, ref)
	b, rerr := os.ReadFile(path)
	if rerr != nil {
		return nil, false, fmt.Errorf("read soul_ref %q: %w", path, rerr)
	}
	return b, true, nil
}

// devicesDir returns the root holding per-device profile folders
// (devices/<type>/{DEVICE,SOUL}.md). Override with DEVICES_DIR. Mirrors openclaw.
func devicesDir() string {
	if d := strings.TrimSpace(os.Getenv("DEVICES_DIR")); d != "" {
		return d
	}
	return "/opt/devices"
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

// isDefaultSoulHeading reports whether trimmed begins with a managed soul template
// heading that must not be preserved as owner content below the device block.
// Mirrors openclaw's isDefaultSoulHeading.
func isDefaultSoulHeading(trimmed string) bool {
	return strings.HasPrefix(trimmed, "# Soul") || strings.HasPrefix(trimmed, "# SOUL.md")
}

// ensureHeartbeatMDBlock injects the knowledge-synthesis block at the top of
// workspace/HEARTBEAT.md. Returns true if modified. Mirrors openclaw.
func (s *PicoclawService) ensureHeartbeatMDBlock() (bool, error) {
	heartbeatFile := filepath.Join(picoclawWorkspaceDir, "HEARTBEAT.md")

	content, err := os.ReadFile(heartbeatFile)
	if err != nil && !os.IsNotExist(err) {
		return false, fmt.Errorf("read HEARTBEAT.md: %w", err)
	}
	text := string(content)

	if strings.Contains(text, heartbeatMDBlock) {
		return false, nil
	}
	if strings.Contains(text, osMandatoryMarker) {
		text = stripMarkedBlock(text)
	}

	output := heartbeatMDBlock + "\n\n" + text
	if err := os.WriteFile(heartbeatFile, []byte(output), 0644); err != nil {
		return false, fmt.Errorf("write HEARTBEAT.md: %w", err)
	}
	slog.Info("injected mandatory block into HEARTBEAT.md", "component", "picoclaw-onboarding", "path", heartbeatFile)
	return true, nil
}

// picoclawBuiltinSkills are PicoClaw's own bundled skills (created by `picoclaw
// onboard`). They have no platform capability mapping in skills.Capability, are
// lightweight + generally useful, so they are ALWAYS kept regardless of device
// capabilities. Only the capability-gated platform skills (the migrated openclaw
// catalog) are pruned. Keep in sync with what `picoclaw onboard` ships.
var picoclawBuiltinSkills = map[string]bool{
	"agent-browser": true,
	"github":        true,
	"hardware":      true,
	"skill-creator": true,
	"summarize":     true,
	"tmux":          true,
	"weather":       true,
}

// pruneUnsupportedSkills removes skill dirs the device can't use. A skill survives
// when it is EITHER (a) supported by this device's capabilities (skills.Supported —
// the same gate openclaw uses) OR (b) a picoclaw built-in (picoclawBuiltinSkills).
// Everything else under workspace/skills is removed. Fail-open: when DEVICE.md
// declares no capabilities, skills.Supported returns the full catalog, so nothing
// capability-gated is pruned. Mirrors openclaw's onboarding prune, but iterates the
// on-disk dirs (picoclaw has extra built-ins outside skills.Catalog) instead of the
// catalog.
func (s *PicoclawService) pruneUnsupportedSkills() {
	skillsDir := filepath.Join(picoclawWorkspaceDir, "skills")
	entries, err := os.ReadDir(skillsDir)
	if err != nil {
		if !os.IsNotExist(err) {
			slog.Warn("prune skills: read dir failed", "component", "picoclaw-onboarding", "error", err)
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
		if keep[name] || picoclawBuiltinSkills[name] {
			continue
		}
		if err := os.RemoveAll(filepath.Join(skillsDir, name)); err != nil {
			slog.Warn("prune unsupported skill failed", "component", "picoclaw-onboarding", "skill", name, "error", err)
			continue
		}
		slog.Info("pruned unsupported skill (capability/built-in gate)", "component", "picoclaw-onboarding", "skill", name)
	}
}

// seedFileIfAbsent writes an embedded file to dst only when dst does not already
// exist (never overwrites — KNOWLEDGE.md is a living doc). Copied from
// internal/openclaw/onboarding.go (package-private there).
func seedFileIfAbsent(efs embed.FS, src, dst string) {
	if _, err := os.Stat(dst); err == nil {
		return // already exists, never overwrite
	}
	data, err := efs.ReadFile(src)
	if err != nil {
		slog.Error("read embedded file failed", "component", "picoclaw-onboarding", "src", src, "error", err)
		return
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0755); err != nil {
		slog.Error("create dir for seed failed", "component", "picoclaw-onboarding", "dst", dst, "error", err)
		return
	}
	if err := os.WriteFile(dst, data, 0644); err != nil {
		slog.Error("write file failed", "component", "picoclaw-onboarding", "dst", dst, "error", err)
		return
	}
	slog.Info("seeded file (initial)", "component", "picoclaw-onboarding", "file", filepath.Base(dst))
}

// stripMarkedBlock removes the block between the marker and the next --- separator.
// Copied from internal/openclaw/onboarding.go (package-private there).
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
