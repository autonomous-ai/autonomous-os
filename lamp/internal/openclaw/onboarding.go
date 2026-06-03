package openclaw

import (
	"embed"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

//go:embed resources/SOUL.md
var soulFS embed.FS

//go:embed resources/KNOWLEDGE.md
var knowledgeFS embed.FS

const (
	skillsBaseURL = "https://storage.googleapis.com/s3-autonomous-upgrade-3/lamp/skills"
	hooksBaseURL  = "https://storage.googleapis.com/s3-autonomous-upgrade-3/lamp/hooks"

	lampMandatoryMarker = "<!-- LAMP DO NOT REMOVE -->"

	agentsMDBlock = `<!-- LAMP DO NOT REMOVE -->
**Hooks** under ` + "`hooks/`" + ` are runtime triggers (handler.ts) that fire automatically on ` + "`message:preprocessed`" + ` before your turn begins. Their HOOK.md files are docstrings describing already-executed handlers — do NOT read them. Skipping HOOK.md reads removes one round-trip per turn with zero behavior change (turn-gate sets busy state, emotion-acknowledge fires the thinking emotion — both server-side, both unconditional).

**MANDATORY (skills):** Before any skill-driven action, determine the skill scope without doing broad filesystem scans. For ordinary chat, simple Q&A, or meta discussion with no action/event/hardware behavior, do NOT read a SKILL.md — answer normally.
  - If the message contains ` + "`[skills: a, b, c]`" + `, treat it as an authoritative whitelist — read ONLY those ` + "`skills/<name>/SKILL.md`" + ` files. Do NOT scan other skill directories "just in case".
  - If no ` + "`[skills:]`" + ` hint is present and the user asks for a concrete action, hardware behavior, sensing/activity/emotion handling, or a specialized workflow, use the injected ` + "`<available_skills>`" + ` descriptions to choose the single most specific matching skill, then read only that SKILL.md.
  - If multiple skills plausibly match, choose the most specific one. If none clearly match, do not read any SKILL.md and answer normally.
  - Never fall back to reading every skill directory. Broad scans are slow and usually reduce quality.

Follow the instructions in whichever file you read.

**Version check:** ` + "`lamp-server --version`" + ` (Lamp), ` + "`openclaw --version`" + ` (OpenClaw), ` + "`curl -s http://127.0.0.1:5001/version`" + ` (LeLamp).

**Session Startup — also read:** ` + "`KNOWLEDGE.md`" + ` (accumulated learnings) in addition to the steps listed below.

**Priority: Skills > Knowledge > memory/*.md > History.** SKILL.md beats EVERYTHING (KNOWLEDGE.md, memory/*.md decisions, history). If memory says NO_REPLY but SKILL says nudge, follow SKILL. KNOWLEDGE.md is your personal observations — it can be wrong. Skills are the source of truth maintained by the developer. If you notice a conflict, update KNOWLEDGE.md to match the skill, not the other way around.

**Memory:** After each turn on any channel (voice, Telegram, or others) that contains something worth remembering (decisions, bugs, insights, new preferences), write it immediately to ` + "`memory/YYYY-MM-DD.md`" + `. Do not wait for heartbeat — context may be dropped before then.

**Memory writes — DESCRIBE, never PRESCRIBE.** Before writing any "decision/rule" to memory/*.md or KNOWLEDGE.md, re-read the relevant SKILL.md. Blanket forms like "X → always Y" / "X → NO_REPLY for all" are frequency disguised as rule — write what happened with conditions, not a blanket ban.

**Don't duplicate JSONL.** Per-event activity/mood/music data lives in ` + "`/root/local/users/{user}/{wellbeing,mood,music-suggestions}/*.jsonl`" + ` and ` + "`/root/local/flow_events_*.jsonl`" + `. If ` + "`cat`" + ` of a JSONL can answer it, DO NOT write to memory. Memory is for cross-day insights only.

**Mood awareness (MANDATORY): Follow Mood skill.**

**User priority (MANDATORY):** When the turn batches multiple messages, ` + "`[user] ...`" + ` messages are direct human input (voice command or typed chat). Always answer the most recent ` + "`[user]`" + ` message first; treat ` + "`[activity]`" + ` / ` + "`[emotion]`" + ` / ` + "`[speech_emotion]`" + ` / ` + "`[ambient]`" + ` / ` + "`[sensing:*]`" + ` as supporting context, never as the primary prompt. A user who asked a question must get their answer even when sensing events queued alongside look more interesting.

---`

	heartbeatMDBlock = `<!-- LAMP DO NOT REMOVE -->
**Knowledge synthesis (once daily at 21:00):** If current time is >= 21:00 AND you have NOT already done this today (check ` + "`KNOWLEDGE.md`" + ` for today's date header), read today's ` + "`memory/YYYY-MM-DD.md`" + `, extract important insights, and append them to ` + "`KNOWLEDGE.md`" + ` under a ` + "`## YYYY-MM-DD`" + ` header. Only write new learnings — do not repeat what is already there. If already done today or before 21:00, skip silently.

---`
)

// hooks is the list of hook names available on CDN.
// Each hook has HOOK.md (metadata) and handler.ts (logic).
var hooks = []string{
	"emotion-acknowledge",
	"turn-gate",
}

// skills is the list of skill names available on CDN.
var skills = []string{
	"audio",
	"camera",
	"connectors",
	"display",
	"emotion",
	"face-enroll",
	"guard",
	"led-control",
	"music",
	"music-suggestion",
	"scene",
	"sensing",
	"sensing-track",
	"servo-control",
	"servo-tracking",
	"voice",
	"wellbeing",
	"mood",
	"speaker-recognizer",
	"user-emotion-detection",
	"habit",
}

// EnsureOnboarding seeds SOUL.md, downloads skills, and injects the mandatory
// block into workspace/AGENTS.md so OpenClaw scans the skills directory.
// IDENTITY.md is managed by OpenClaw itself (created during openclaw onboard).
func (s *Service) EnsureOnboarding() error {
	workspace := filepath.Join(s.config.OpenclawConfigDir, "workspace")
	if err := os.MkdirAll(workspace, 0755); err != nil {
		return fmt.Errorf("create workspace dir: %w", err)
	}

	needRestart := false

	// Inject SOUL.md core block (owner-editable content stays below the block)
	if modified, err := s.ensureSoulMDBlock(); err != nil {
		slog.Error("ensure SOUL.md block failed", "component", "onboarding", "error", err)
	} else if modified {
		needRestart = true
	}

	// OLD (kept for rollback): full-file overwrite of SOUL.md from embedded binary.
	// Replaced by ensureSoulMDBlock above so owner edits below the marker survive.
	// if changed := seedFile(soulFS, "resources/SOUL.md", filepath.Join(workspace, "SOUL.md")); changed {
	// 	needRestart = true
	// }

	// Download skills from CDN
	skillsDir := filepath.Join(workspace, "skills")
	if err := os.MkdirAll(skillsDir, 0755); err != nil {
		return fmt.Errorf("create skills dir: %w", err)
	}
	// Ensure skill directories exist
	for _, name := range skills {
		if err := os.MkdirAll(filepath.Join(skillsDir, name), 0755); err != nil {
			slog.Error("mkdir failed", "component", "onboarding", "dir", name, "error", err)
		}
	}
	changedSkills := s.downloadSkills()

	// Download hooks from CDN
	hooksDir := filepath.Join(workspace, "hooks")
	if err := os.MkdirAll(hooksDir, 0755); err != nil {
		return fmt.Errorf("create hooks dir: %w", err)
	}
	hookFiles := []string{"HOOK.md", "handler.ts"}
	for _, name := range hooks {
		dir := filepath.Join(hooksDir, name)
		if err := os.MkdirAll(dir, 0755); err != nil {
			slog.Error("mkdir failed", "component", "onboarding", "dir", dir, "error", err)
			continue
		}
		for _, file := range hookFiles {
			dst := filepath.Join(dir, file)
			url := fmt.Sprintf("%s/%s/%s", hooksBaseURL, name, file)
			changed, err := downloadFile(url, dst)
			if err != nil {
				slog.Error("download hook file failed", "component", "onboarding", "hook", name, "file", file, "error", err)
				continue
			}
			if changed {
				needRestart = true
			}
		}
		slog.Info("seeded hook", "component", "onboarding", "hook", name)
	}

	// Seed KNOWLEDGE.md template only if the file does not already exist (living doc)
	seedFileIfAbsent(knowledgeFS, "resources/KNOWLEDGE.md", filepath.Join(workspace, "KNOWLEDGE.md"))

	// Ensure AGENTS.md has mandatory block
	if modified, err := s.ensureAgentsMDBlock(); err != nil {
		slog.Error("ensure AGENTS.md block failed", "component", "onboarding", "error", err)
	} else if modified {
		needRestart = true
	}

	// Ensure HEARTBEAT.md has knowledge-synthesis block
	if modified, err := s.ensureHeartbeatMDBlock(); err != nil {
		slog.Error("ensure HEARTBEAT.md block failed", "component", "onboarding", "error", err)
	} else if modified {
		needRestart = true
	}

	// Ensure all hooks are registered in openclaw.json hooks.internal.entries
	if hooksAdded, err := s.ensureHooksRegistered(hooks); err != nil {
		slog.Error("ensure hooks registered failed", "component", "onboarding", "error", err)
	} else if hooksAdded {
		needRestart = true
	}

	// Ensure logging config is present in openclaw.json
	if loggingAdded, err := s.ensureLoggingConfig(); err != nil {
		slog.Error("ensure logging config failed", "component", "onboarding", "error", err)
	} else if loggingAdded {
		needRestart = true
	}

	// Ensure agent defaults (compaction, bootstrap limits, caching)
	if defaultsPatched, err := s.ensureAgentDefaults(); err != nil {
		slog.Error("ensure agent defaults failed", "component", "onboarding", "error", err)
	} else if defaultsPatched {
		needRestart = true
	}

	// Ensure gateway controlUi allows external origins (nginx proxy)
	if controlUIAdded, err := s.ensureControlUIConfig(); err != nil {
		slog.Error("ensure controlUi config failed", "component", "onboarding", "error", err)
	} else if controlUIAdded {
		needRestart = true
	}

	// Pin messages.queue.mode=steer so Lamp's concurrent producers (sensing
	// drains, voice, Telegram, web chat) batch into the active turn at the
	// next model boundary instead of fanning out as serialized followup turns.
	if queueAdded, err := s.ensureMessagesQueueConfig(); err != nil {
		slog.Error("ensure messages.queue config failed", "component", "onboarding", "error", err)
	} else if queueAdded {
		needRestart = true
	}

	// Restart OpenClaw if non-skill files changed (SOUL.md, AGENTS.md, hooks, config)
	if needRestart {
		slog.Info("restarting OpenClaw to pick up changes", "component", "onboarding")
		if err := restartOpenclawGateway(); err != nil {
			return fmt.Errorf("restart openclaw after onboarding: %w", err)
		}
		slog.Info("OpenClaw restarted successfully", "component", "onboarding")
	}

	// For changed skills, tell the agent to re-read them (no restart needed).
	// This runs after restart (if any) so WS is connected.
	s.notifySkillChanges(changedSkills)

	return nil
}

// ensureHooksRegistered adds any missing hooks to openclaw.json hooks.internal.entries.
// Returns true if the file was modified.
func (s *Service) ensureHooksRegistered(hookNames []string) (bool, error) {
	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	configBytes, err := os.ReadFile(configPath)
	if err != nil {
		return false, fmt.Errorf("read openclaw.json: %w", err)
	}
	var configData map[string]interface{}
	if err := json.Unmarshal(configBytes, &configData); err != nil {
		return false, fmt.Errorf("parse openclaw.json: %w", err)
	}

	hooksMap := ensureMap(configData, "hooks")
	internalMap := ensureMap(hooksMap, "internal")
	if _, ok := internalMap["enabled"]; !ok {
		internalMap["enabled"] = true
	}
	entriesMap := ensureMap(internalMap, "entries")

	changed := false
	for _, name := range hookNames {
		if _, exists := entriesMap[name]; !exists {
			entriesMap[name] = map[string]interface{}{"enabled": true}
			changed = true
			slog.Info("registered hook in openclaw.json", "component", "onboarding", "hook", name)
		}
	}
	if !changed {
		return false, nil
	}

	outBytes, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return false, fmt.Errorf("marshal openclaw.json: %w", err)
	}
	if err := os.WriteFile(configPath, outBytes, 0600); err != nil {
		return false, fmt.Errorf("write openclaw.json: %w", err)
	}
	return true, nil
}

// ensureAgentsMDBlock injects the mandatory skills block into AGENTS.md.
// Returns true if the file was modified.
func (s *Service) ensureAgentsMDBlock() (bool, error) {
	agentsFile := filepath.Join(s.config.OpenclawConfigDir, "workspace", "AGENTS.md")

	// If AGENTS.md is missing, run `openclaw setup` to regenerate the base template
	// before injecting the mandatory block. This preserves the full default content
	// (session startup instructions, memory rules, etc.) instead of writing to an empty file.
	if _, err := os.Stat(agentsFile); os.IsNotExist(err) {
		slog.Info("AGENTS.md missing, running openclaw setup to regenerate", "component", "onboarding")
		if out, err := exec.Command("openclaw", "setup").CombinedOutput(); err != nil {
			slog.Warn("openclaw setup failed, will inject into empty file", "component", "onboarding", "error", err, "output", strings.TrimSpace(string(out)))
		}
	}

	content, err := os.ReadFile(agentsFile)
	if err != nil && !os.IsNotExist(err) {
		return false, fmt.Errorf("read AGENTS.md: %w", err)
	}

	text := string(content)

	// Already has the exact current block → skip
	if strings.Contains(text, agentsMDBlock) {
		slog.Debug("AGENTS.md already has current mandatory block, skipping", "component", "onboarding")
		return false, nil
	}

	// Remove old block (with or without marker) before injecting current version
	if strings.Contains(text, lampMandatoryMarker) {
		text = stripMarkedBlock(text)
	} else {
		text = stripLegacyMandatoryBlock(text)
	}

	// Find "Your workspace" line and inject block below it
	lines := strings.Split(text, "\n")
	var result []string
	injected := false

	for _, line := range lines {
		result = append(result, line)
		if !injected && strings.Contains(strings.ToLower(line), "your workspace") {
			result = append(result, agentsMDBlock)
			injected = true
		}
	}

	// If "Your workspace" not found, prepend to top of file
	if !injected {
		slog.Debug("'Your workspace' not found in AGENTS.md, prepending block", "component", "onboarding")
		result = append([]string{agentsMDBlock, ""}, result...)
	}

	output := strings.Join(result, "\n")
	if err := os.WriteFile(agentsFile, []byte(output), 0644); err != nil {
		return false, fmt.Errorf("write AGENTS.md: %w", err)
	}

	slog.Info("injected mandatory block into AGENTS.md", "component", "onboarding", "path", agentsFile)
	return true, nil
}

// ensureSoulMDBlock wraps the embedded SOUL.md as a marker-delimited core block
// at the top of workspace/SOUL.md. Anything the owner writes below the closing
// `---` is preserved on subsequent onboarding runs, mirroring the AGENTS.md /
// HEARTBEAT.md pattern. Returns true if the file was modified.
func (s *Service) ensureSoulMDBlock() (bool, error) {
	soulFile := filepath.Join(s.config.OpenclawConfigDir, "workspace", "SOUL.md")

	coreContent, err := soulFS.ReadFile("resources/SOUL.md")
	if err != nil {
		return false, fmt.Errorf("read embedded SOUL.md: %w", err)
	}
	soulMDBlock := lampMandatoryMarker + "\n" + strings.TrimSpace(string(coreContent)) + "\n---"

	content, err := os.ReadFile(soulFile)
	if err != nil && !os.IsNotExist(err) {
		return false, fmt.Errorf("read SOUL.md: %w", err)
	}
	text := string(content)

	// Fast path: file already contains the current block verbatim. Without
	// this, the strip/rejoin path below re-introduces an extra blank line
	// after `---` on every run, so output != content and we keep rewriting
	// SOUL.md (and restarting OpenClaw) on every Lamp boot.
	if strings.Contains(text, soulMDBlock) {
		return false, nil
	}

	// Strip any prior marker block first so the legacy-seed heuristic below
	// only sees whatever was below the closing `---`.
	if strings.Contains(text, lampMandatoryMarker) {
		text = stripMarkedBlock(text)
	}

	// Legacy migration: before the marker block existed, onboarding overwrote
	// SOUL.md with the embedded core verbatim every run via seedFile, so
	// unmodified devices have content shaped like the embedded core. The
	// previous strict-equality check (file == current coreContent) silently
	// failed whenever the embedded core had drifted since the device's last
	// seed (e.g. soft-door / language-mirror / no-JSONL-duplicate updates),
	// which preserved the stale core as fake "owner edits" and duplicated
	// it on top of the new marker block. The same shape — current marker
	// block followed by another `# Soul` block — also persists on devices
	// that already ran the broken migration; stripping the marker first
	// lets this branch self-heal them on the next onboarding run.
	//
	// Detect any legacy-seed shape by the `# Soul` heading at the start of
	// the remaining text. If the owner has added their own `## Personal`
	// section below it, keep only that section; otherwise discard entirely.
	trimmed := strings.TrimLeft(text, " \t\r\n")
	if strings.HasPrefix(trimmed, "# Soul") {
		if idx := strings.Index(text, "## Personal"); idx >= 0 {
			text = text[idx:]
		} else {
			text = ""
		}
	}

	var output string
	if strings.TrimSpace(text) == "" {
		// First install or clean migration → seed an owner-editable Personal section.
		output = soulMDBlock + "\n\n## Personal\n\n_Owner-editable. Add notes about yourself, family, routines, or personality tweaks for Lamp here. The block above is managed by Lamp and will be refreshed on each update — keep your edits in this section._\n"
	} else {
		output = soulMDBlock + "\n\n" + text
	}

	if output == string(content) {
		slog.Debug("SOUL.md already in canonical shape, skipping", "component", "onboarding")
		return false, nil
	}

	if err := os.WriteFile(soulFile, []byte(output), 0644); err != nil {
		return false, fmt.Errorf("write SOUL.md: %w", err)
	}

	slog.Info("injected core block into SOUL.md", "component", "onboarding", "path", soulFile)
	return true, nil
}

// ensureHeartbeatMDBlock injects the knowledge-synthesis block into HEARTBEAT.md.
// Returns true if the file was modified.
func (s *Service) ensureHeartbeatMDBlock() (bool, error) {
	heartbeatFile := filepath.Join(s.config.OpenclawConfigDir, "workspace", "HEARTBEAT.md")

	content, err := os.ReadFile(heartbeatFile)
	if err != nil && !os.IsNotExist(err) {
		return false, fmt.Errorf("read HEARTBEAT.md: %w", err)
	}

	text := string(content)

	// Already has the exact current block → skip
	if strings.Contains(text, heartbeatMDBlock) {
		slog.Debug("HEARTBEAT.md already has current mandatory block, skipping", "component", "onboarding")
		return false, nil
	}

	// Remove old block if marker exists, then inject current version
	if strings.Contains(text, lampMandatoryMarker) {
		text = stripMarkedBlock(text)
	}

	// Prepend block at the top of the file
	output := heartbeatMDBlock + "\n\n" + text
	if err := os.WriteFile(heartbeatFile, []byte(output), 0644); err != nil {
		return false, fmt.Errorf("write HEARTBEAT.md: %w", err)
	}

	slog.Info("injected mandatory block into HEARTBEAT.md", "component", "onboarding", "path", heartbeatFile)
	return true, nil
}

// stripMarkedBlock removes the block between the marker (<!-- LAMP DO NOT REMOVE -->)
// and the next --- separator.
func stripMarkedBlock(text string) string {
	lines := strings.Split(text, "\n")
	var cleaned []string
	skip := false
	for _, line := range lines {
		trimmed := strings.TrimSpace(line)
		if trimmed == lampMandatoryMarker {
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

// stripLegacyMandatoryBlock removes the old MANDATORY block that was injected
// before any marker (<!-- LAMP DO NOT REMOVE -->) was introduced.
func stripLegacyMandatoryBlock(text string) string {
	lines := strings.Split(text, "\n")
	var cleaned []string
	skip := false
	for _, line := range lines {
		trimmed := strings.TrimSpace(line)
		// Detect start of legacy block: starts with **MANDATORY:** but no marker above
		if !skip && strings.HasPrefix(trimmed, "**MANDATORY:**") {
			skip = true
			continue
		}
		// End of legacy block: next non-empty line that doesn't look like continuation
		if skip {
			if trimmed == "" || trimmed == "---" {
				skip = false
				// Keep the separator/blank line
				cleaned = append(cleaned, line)
			}
			// Skip continuation lines of the old block
			continue
		}
		cleaned = append(cleaned, line)
	}
	return strings.Join(cleaned, "\n")
}

// ensureLoggingConfig adds the logging block to openclaw.json if it is missing.
// Returns true if the file was modified.
func (s *Service) ensureLoggingConfig() (bool, error) {
	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	configBytes, err := os.ReadFile(configPath)
	if err != nil {
		return false, fmt.Errorf("read openclaw.json: %w", err)
	}
	var configData map[string]interface{}
	if err := json.Unmarshal(configBytes, &configData); err != nil {
		return false, fmt.Errorf("parse openclaw.json: %w", err)
	}

	if _, ok := configData["logging"]; ok {
		return false, nil
	}

	configData["logging"] = map[string]interface{}{
		"consoleStyle": "pretty",
		"file":         "/var/log/openclaw/lamp.log",
		"level":        "debug",
		"consoleLevel": "debug",
	}

	outBytes, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return false, fmt.Errorf("marshal openclaw.json: %w", err)
	}
	if err := os.WriteFile(configPath, outBytes, 0600); err != nil {
		return false, fmt.Errorf("write openclaw.json: %w", err)
	}
	slog.Info("added logging config to openclaw.json", "component", "onboarding")
	return true, nil
}

// ensureControlUIConfig pins gateway.controlUi to local-only defaults so the
// Control UI handshake only accepts loopback origins on plain HTTP. Combined
// with nginx `/gw/` allow 127.0.0.1; deny all; (F6), the gateway is reachable
// only from on-device callers (SSH port-forward, on-device browser).
//
// Defaults:
//   - allowedOrigins = ["http://127.0.0.1", "http://localhost"]
//   - allowInsecureAuth = false
//
// Migration: devices originally provisioned with the loose defaults
// (allowedOrigins=["*"], allowInsecureAuth=true — used before F6 closed LAN
// access at nginx) are upgraded automatically here. Operators who set custom
// origins are left untouched.
func (s *Service) ensureControlUIConfig() (bool, error) {
	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	configBytes, err := os.ReadFile(configPath)
	if err != nil {
		return false, fmt.Errorf("read openclaw.json: %w", err)
	}
	var configData map[string]interface{}
	if err := json.Unmarshal(configBytes, &configData); err != nil {
		return false, fmt.Errorf("parse openclaw.json: %w", err)
	}

	gw, ok := configData["gateway"].(map[string]interface{})
	if !ok {
		return false, nil
	}

	cu, _ := gw["controlUi"].(map[string]interface{})
	if cu == nil {
		cu = map[string]interface{}{}
		gw["controlUi"] = cu
	}

	strictOrigins := []string{"http://127.0.0.1", "http://localhost"}
	changed := false

	switch v := cu["allowedOrigins"].(type) {
	case nil:
		cu["allowedOrigins"] = strictOrigins
		changed = true
	case []interface{}:
		// Migrate the historical loose default (exactly ["*"]) to strict.
		// Custom operator lists (any other shape) are preserved.
		if len(v) == 1 {
			if s0, ok := v[0].(string); ok && s0 == "*" {
				cu["allowedOrigins"] = strictOrigins
				changed = true
			}
		}
	}

	switch v := cu["allowInsecureAuth"].(type) {
	case nil:
		cu["allowInsecureAuth"] = false
		changed = true
	case bool:
		// Loopback HTTP works without this flag — nginx /gw/ already restricts
		// to loopback peers (F6), so non-loopback HTTP can never reach the
		// handshake. Safe to flip true → false unconditionally.
		if v {
			cu["allowInsecureAuth"] = false
			changed = true
		}
	}

	if !changed {
		return false, nil
	}

	outBytes, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return false, fmt.Errorf("marshal openclaw.json: %w", err)
	}
	if err := os.WriteFile(configPath, outBytes, 0600); err != nil {
		return false, fmt.Errorf("write openclaw.json: %w", err)
	}
	slog.Info("tightened controlUi config in openclaw.json", "component", "onboarding")
	return true, nil
}

// ensureMessagesQueueConfig pins messages.queue.mode to "steer" so concurrent
// messages (sensing drains, voice + Telegram interleave) get batched into the
// active turn at the next model boundary instead of spawning serialized
// followup turns. Lamp has multiple producers (sensing handler, voice, web
// chat, Telegram) feeding agent:main:main; legacy "queue" mode runs each as
// its own turn, missing batch opportunities the steer path can collapse.
//
// Trade-offs are tracked in issue #48003 (steer fallback to followup on Pi
// main session via KeyedAsyncQueue) and the ReplyRunAlreadyActive race seen
// on 5.2 — verify on 5.7+ before relying on steer batching savings.
//
// Always overwrites — Lamp owns this config knob; an operator who flips it
// to "queue" will see Lamp correct on the next boot.
func (s *Service) ensureMessagesQueueConfig() (bool, error) {
	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	configBytes, err := os.ReadFile(configPath)
	if err != nil {
		return false, fmt.Errorf("read openclaw.json: %w", err)
	}
	var configData map[string]interface{}
	if err := json.Unmarshal(configBytes, &configData); err != nil {
		return false, fmt.Errorf("parse openclaw.json: %w", err)
	}

	messages, _ := configData["messages"].(map[string]interface{})
	if messages == nil {
		messages = map[string]interface{}{}
		configData["messages"] = messages
	}
	queue, _ := messages["queue"].(map[string]interface{})
	if queue == nil {
		queue = map[string]interface{}{}
		messages["queue"] = queue
	}
	if v, _ := queue["mode"].(string); v == "steer" {
		return false, nil
	}
	queue["mode"] = "steer"

	outBytes, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return false, fmt.Errorf("marshal openclaw.json: %w", err)
	}
	if err := os.WriteFile(configPath, outBytes, 0600); err != nil {
		return false, fmt.Errorf("write openclaw.json: %w", err)
	}
	slog.Info("pinned messages.queue.mode=steer in openclaw.json", "component", "onboarding")
	return true, nil
}

// downloadFile fetches url and writes it to dst. Returns true if the file content changed.
func downloadFile(url, dst string) (bool, error) {
	client := &http.Client{Timeout: 30 * time.Second}
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return false, err
	}
	req.Header.Set("Cache-Control", "no-cache")
	resp, err := client.Do(req)
	if err != nil {
		return false, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return false, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	newData, err := io.ReadAll(resp.Body)
	if err != nil {
		return false, err
	}
	existing, err := os.ReadFile(dst)
	if err == nil && string(existing) == string(newData) {
		return false, nil
	}
	if err := os.WriteFile(dst, newData, 0644); err != nil {
		return false, err
	}
	return true, nil
}

// seedFileIfAbsent writes the embedded file to dst only if dst does not already exist.
// Used for living documents (e.g. KNOWLEDGE.md) that accumulate data over time.
func seedFileIfAbsent(efs embed.FS, src, dst string) {
	if _, err := os.Stat(dst); err == nil {
		return // already exists, never overwrite
	}
	data, err := efs.ReadFile(src)
	if err != nil {
		slog.Error("read embedded file failed", "component", "onboarding", "src", src, "error", err)
		return
	}
	if err := os.WriteFile(dst, data, 0644); err != nil {
		slog.Error("write file failed", "component", "onboarding", "dst", dst, "error", err)
		return
	}
	slog.Info("seeded file (initial)", "component", "onboarding", "file", filepath.Base(dst))
}

// seedFile writes the embedded file to dst. Returns true if the file content changed.
func seedFile(efs embed.FS, src, dst string) bool {
	data, err := efs.ReadFile(src)
	if err != nil {
		slog.Error("read embedded file failed", "component", "onboarding", "src", src, "error", err)
		return false
	}
	existing, err := os.ReadFile(dst)
	if err == nil && string(existing) == string(data) {
		return false
	}
	if err := os.WriteFile(dst, data, 0644); err != nil {
		slog.Error("write file failed", "component", "onboarding", "dst", dst, "error", err)
		return false
	}
	slog.Info("seeded file", "component", "onboarding", "file", filepath.Base(dst))
	return true
}

// ensureAgentDefaults patches agents.defaults in openclaw.json with performance config.
// Returns true if the file was modified.
func (s *Service) ensureAgentDefaults() (bool, error) {
	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	configBytes, err := os.ReadFile(configPath)
	if err != nil {
		return false, fmt.Errorf("read openclaw.json: %w", err)
	}
	var configData map[string]interface{}
	if err := json.Unmarshal(configBytes, &configData); err != nil {
		return false, fmt.Errorf("parse openclaw.json: %w", err)
	}

	agentsMap := ensureMap(configData, "agents")
	defaultsMap := ensureMap(agentsMap, "defaults")

	changed := false

	// Compaction
	// reserveTokensFloor=5000: keep safeguard only as a last-resort guard near
	// the model context limit (~195k for 200k models). Previously 80000, which
	// made OpenClaw fire compact at ~120k actual context — same range Lamp's
	// /new RPC trigger fires (chat.history TotalTokens > 80k undercounts ~35k),
	// so the two layers raced and produced the 30-60s compact freeze that
	// /new was supposed to avoid.
	compactionMap := ensureMap(defaultsMap, "compaction")
	if v, _ := compactionMap["reserveTokensFloor"].(float64); v != 5000 {
		compactionMap["reserveTokensFloor"] = 5000
		changed = true
	}
	if v, _ := compactionMap["mode"].(string); v != "safeguard" {
		compactionMap["mode"] = "safeguard"
		changed = true
	}

	// Bootstrap limits
	if v, _ := defaultsMap["bootstrapMaxChars"].(float64); v != 12000 {
		defaultsMap["bootstrapMaxChars"] = 12000
		changed = true
	}
	if v, _ := defaultsMap["bootstrapTotalMaxChars"].(float64); v != 30000 {
		defaultsMap["bootstrapTotalMaxChars"] = 30000
		changed = true
	}

	// /think default — favor low latency over deep reasoning for voice turns.
	// Per-message override (`/think medium`) still wins; this only sets the
	// fallback when neither session nor inline directive specify a level.
	if v, _ := defaultsMap["thinkingDefault"].(string); v != "low" {
		defaultsMap["thinkingDefault"] = "low"
		changed = true
	}

	// Cache retention (Claude only) + /fast default = on (priority tier) on all known models.
	// `fastMode=true` maps to provider-specific priority routing — `service_tier=priority`
	// on OpenAI/Codex; no-op on providers that don't expose a priority tier.
	modelsMap := ensureMap(defaultsMap, "models")
	// Autonomous-backed list comes from the live API (single source of truth);
	// non-autonomous entries (e.g. openai-codex) are appended manually because
	// they are not driven by ModelsAPIURL. Fail-soft on API failure: skip the
	// autonomous portion this boot, preserve existing on-disk tuning, retry
	// next boot.
	var knownModels []string
	if resp, err := FetchModelsFromAPI(); err != nil {
		slog.Warn("ensureAgentDefaults: fetch autonomous models failed, skipping",
			"component", "onboarding", "err", err)
	} else {
		for _, m := range resp.Models {
			knownModels = append(knownModels, agentModelKey(m))
		}
	}
	knownModels = append(knownModels, "openai-codex/gpt-5.5")
	for _, modelKey := range knownModels {
		m, ok := modelsMap[modelKey].(map[string]interface{})
		if !ok {
			m = map[string]interface{}{}
			modelsMap[modelKey] = m
			changed = true
		}
		params := ensureMap(m, "params")
		// Contains (not HasPrefix) so "{provider}/claude-..." also matches.
		if strings.Contains(modelKey, "claude-") {
			if v, _ := params["cacheRetention"].(string); v != "short" {
				params["cacheRetention"] = "short"
				changed = true
			}
		}
		if v, _ := params["fastMode"].(bool); !v {
			params["fastMode"] = true
			changed = true
		}
		m["params"] = params
		modelsMap[modelKey] = m
	}

	// Sync reasoning field on all provider model entries with current disable_thinking config.
	// Ensures manual edits to config.json take effect on next boot without needing API call.
	disableThinking := s.config.LLMThinkingDisabled()
	wantReasoning := !disableThinking
	if topModels, ok := configData["models"].(map[string]interface{}); ok {
		if providers, ok := topModels["providers"].(map[string]interface{}); ok {
			for _, provider := range providers {
				if p, ok := provider.(map[string]interface{}); ok {
					if modelsList, ok := p["models"].([]interface{}); ok {
						for _, entry := range modelsList {
							if m, ok := entry.(map[string]interface{}); ok {
								if curr, _ := m["reasoning"].(bool); curr != wantReasoning {
									m["reasoning"] = wantReasoning
									changed = true
								}
							}
						}
					}
				}
			}
		}
	}

	if !changed {
		return false, nil
	}

	outBytes, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return false, fmt.Errorf("marshal openclaw.json: %w", err)
	}
	if err := os.WriteFile(configPath, outBytes, 0600); err != nil {
		return false, fmt.Errorf("write openclaw.json: %w", err)
	}
	slog.Info("patched agent defaults in openclaw.json", "component", "onboarding")
	return true, nil
}
