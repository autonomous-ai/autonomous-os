package claudecode

import (
	"log"
	"os/exec"
	"time"

	"go.autonomous.ai/os/internal/skills"
	"go.autonomous.ai/os/lib/osreset"
)

// ResetAgent performs the Claude Code factory-reset wipe. The factory-reset flow
// (server/system/factoryreset.go) resolves the active gateway and calls this on
// it — adding a backend means implementing ResetAgent, not editing a switch.
//
// What is wiped vs kept:
//
//	WIPED — user data + creds:
//	  /root/.claudecode/workspace     persona, memory, CLAUDE.md, .mcp.json
//	  /root/.claudecode/.env          ANTHROPIC_* creds + channel launch flags
//	  /root/.claudecode/session.json  bridge session-resume state
//	  /root/.claude/skills            device skills (user-scoped — see claudecodeSkillsDir)
//	  /root/.claude/CLAUDE.md         user-level OS block (device-wide connector rules)
//	  /root/.claude/projects          Claude Code conversation transcripts
//	  /root/.claude/channels          channel tokens, allowlists, inbox media
//	  /root/.claude/todos             per-session todo state
//	  /root/.claude/history.jsonl     prompt history
//
//	KEPT — installed software (install.sh only re-runs on a failed verify):
//	  the claude CLI, /root/.claude.json + global settings (the bridge itself
//	  ships inside the os-server binary — nothing on disk to wipe or keep; no
//	  bun/plugins anymore: telegram + discord are device-owned loops).
//
// Everything wiped has a restore path that runs after the reset: presync +
// EnsureOnboarding rebuild .env from the (re-entered) config.json (the
// device-owned channel loops need nothing beyond config), and
// ensureSkills re-downloads the skill set from the CDN — see
// docs/agentic/adding-agent-runtime.md §7.
func (s *ClaudeCodeService) ResetAgent() error {
	wipeClaudeCodeState()
	return nil
}

// stopVerifyTimeout caps how long we wait for the claudecode unit to actually
// leave the active state after `systemctl stop`.
const stopVerifyTimeout = 5 * time.Second

var claudecodeWipePaths = []string{
	claudecodeHome + "/workspace",
	claudecodeHome + "/.env",
	claudecodeHome + "/session.json",
	// Skills + the user-level OS block live outside the workspace (user scope, so
	// coding sessions in any cwd see them) — wipe them explicitly or a factory
	// reset would leave stale skills behind. ensureSkills/ensureClaudeMDBlock
	// rebuild both on the next onboarding pass.
	//
	// The SHARED store is what actually holds the skills; claudecodeSkillsDir is only
	// a symlink into it (ensureSkillsLink). Wipe both, or the reset would leave every
	// skill on disk behind a dangling link.
	skills.StoreDir,
	claudecodeSkillsDir,
	claudeUserDir + "/CLAUDE.md",
	"/root/.claude/projects",
	"/root/.claude/channels",
	"/root/.claude/todos",
	"/root/.claude/history.jsonl",
	// claude.ai subscription credentials from the login flow — creds must not
	// survive a factory reset (config.json's claude_code_oauth_token is wiped
	// with the config; this is the on-disk half).
	"/root/.claude/.credentials.json",
}

// wipeClaudeCodeState runs the reset flow: stop + verify the bridge (it holds
// the Claude child that would otherwise re-create session state mid-wipe),
// disable the unit (no auto-start into a wiped workspace; EnsureOnboarding /
// switch-runtime re-enables), then remove the state paths.
func wipeClaudeCodeState() {
	log.Printf("[factory-reset/claudecode] step 1/3 — systemctl stop claudecode")
	if out, err := exec.Command("systemctl", "stop", claudecodeUnitName).CombinedOutput(); err != nil {
		log.Printf("[factory-reset/claudecode] step 1/3 — stop error: %v — %s", err, string(out))
	}
	if waitForServiceStop(claudecodeUnitName, stopVerifyTimeout) {
		log.Printf("[factory-reset/claudecode] step 1/3 — claudecode confirmed inactive")
	} else {
		log.Printf("[factory-reset/claudecode] step 1/3 — WARNING claudecode still active after %s — wipe may race the running bridge", stopVerifyTimeout)
	}

	log.Printf("[factory-reset/claudecode] step 2/3 — systemctl disable claudecode")
	if out, err := exec.Command("systemctl", "disable", claudecodeUnitName).CombinedOutput(); err != nil {
		log.Printf("[factory-reset/claudecode] step 2/3 — disable error: %v — %s", err, string(out))
	}

	log.Printf("[factory-reset/claudecode] step 3/3 — wiping %d paths", len(claudecodeWipePaths))
	for _, p := range claudecodeWipePaths {
		osreset.WipePath("[factory-reset/claudecode]", p)
	}
}

// isServiceActive returns true if `systemctl is-active <unit>` exits 0.
func isServiceActive(unit string) bool {
	return exec.Command("systemctl", "is-active", "--quiet", unit).Run() == nil
}

// waitForServiceStop polls is-active until the unit is no longer active or the
// timeout elapses.
func waitForServiceStop(unit string, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for {
		if !isServiceActive(unit) {
			return true
		}
		if time.Now().After(deadline) {
			return false
		}
		time.Sleep(200 * time.Millisecond)
	}
}
