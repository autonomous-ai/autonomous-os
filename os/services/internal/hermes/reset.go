package hermes

import (
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"go.autonomous.ai/os/lib/osreset"
)

// ResetAgent performs the Hermes factory-reset wipe. The factory-reset flow
// (server/system/factoryreset.go) resolves the active gateway and calls this on
// it — so adding a backend means implementing ResetAgent, not editing a switch.
func (s *HermesService) ResetAgent() error {
	wipeHermesState()
	return nil
}

// stopVerifyTimeout caps how long we wait for hermes-gateway to actually leave
// the active state after `systemctl stop`.
const stopVerifyTimeout = 5 * time.Second

// isServiceActive returns true if `systemctl is-active <unit>` exits 0 (active).
// All other states (inactive, failed, unknown, activating) are treated as "not
// active" — safe to wipe.
func isServiceActive(unit string) bool {
	return exec.Command("systemctl", "is-active", "--quiet", unit).Run() == nil
}

// waitForServiceStop polls is-active until the unit is no longer active or the
// timeout elapses. Returns true if the service is confirmed stopped in the window.
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

// hermesWipeDirs are Hermes state subdirs we recursively remove on factory
// reset. Top-level files inside hermesHome are handled separately by the
// directory sweep (see hermesKeepFiles) — anything NOT in this list and NOT in
// hermesKeepFiles stays untouched (skills/, bin/, audio_cache/, pairing/, …).
var hermesWipeDirs = []string{
	hermesHome + "/sessions",                // conversation session state
	hermesHome + "/memories",                // semantic memory store
	hermesHome + "/tasks",                   // background task runs
	hermesHome + "/subagents",               // subagent history
	hermesHome + "/checkpoints",             // run checkpoints
	hermesHome + "/logs",                    // runtime logs
	hermesHome + "/.cache",                  // cache dir (dotted)
	hermesHome + "/cron",                    // scheduled jobs
	hermesHome + "/cache",                   // runtime cache (non-dotted, see tree)
	hermesHome + "/gateway",                 // gateway runtime state dir
	hermesHome + "/migration",               // migration history
	hermesHome + "/skills/openclaw-imports", // CDN-downloaded user-defined skills
	hermesHome + "/skills/audio_cache",      // audio cache
	hermesHome + "/skills/image_cache",      // image cache
}

// hermesKeepFiles is the allow-list of TOP-LEVEL FILES (not dirs) under
// hermesHome that survive factory reset. The sweep removes every regular file
// at hermesHome whose name is NOT a key here.
//
//   - .env, config.yaml: reset in place by `hermes setup --reset` (Step 3)
//   - auth.lock:         lock file — leave for daemon to manage on restart
//   - SOUL.md:           kept on disk but overwritten to a blank template
//     after the sweep (so the persona resets without orphaning the file).
//
// Anything else at top level (DB triples like state.db / state.db-shm /
// state.db-wal, response_store.*, kanban.*, auth.json, gateway.pid,
// gateway.lock, gateway_state.json, channel_directory.json,
// config.yaml.bak.*, *_cache.json, image_cacheon, …) is swept out.
var hermesKeepFiles = map[string]bool{
	".env":        true,
	"config.yaml": true,
	"auth.lock":   true,
	"SOUL.md":     true,
}

// hermesSoulTemplate is the blank-slate content written to SOUL.md after the
// sweep. Mirrors the openclaw factoryreset pattern of "wipe persona but keep
// a stub the agent can rebuild from".
const hermesSoulTemplate = "# Hermes Agent Persona\n"

// wipeHermesState runs the Hermes reset flow. Unlike openclaw, Hermes has no
// single "reset everything" CLI. Daemon MUST die before we touch state files,
// otherwise it holds SQLite write handles + re-creates wiped paths on its next
// write tick. Order:
//  1. hermes gateway stop                       (kill daemon first)
//  2. systemctl stop hermes-gateway + verify    (kill any systemd-supervised process)
//  3. hermes setup --reset --non-interactive    (now-safe config.yaml/.env reset)
//  4. systemctl disable hermes-gateway          (no auto-start; SetupAgent re-enables)
//  5. surgical rm                               (dirs + top-level file sweep + SOUL.md template)
//
// `~/.hermes/.env` and `~/.hermes/config.yaml` are NOT rm'd — Step 3 resets
// them in place to defaults. `~/.hermes/SOUL.md` is overwritten in Step 5 with
// a blank template.
func wipeHermesState() {
	// Step 1: stop hermes gateway.
	log.Printf("[factory-reset/hermes] step 1/5 — hermes gateway stop")
	if out, err := exec.Command("hermes", "gateway", "stop").CombinedOutput(); err != nil {
		log.Printf("[factory-reset/hermes] step 1/5 — hermes gateway stop error: %v — %s", err, strings.TrimSpace(string(out)))
	} else {
		log.Printf("[factory-reset/hermes] step 1/5 — hermes gateway stopped")
	}

	// Step 2: stop hermes-gateway systemd unit.
	log.Printf("[factory-reset/hermes] step 2/5 — systemctl stop hermes-gateway")
	if out, err := exec.Command("systemctl", "stop", "hermes-gateway").CombinedOutput(); err != nil {
		log.Printf("[factory-reset/hermes] step 2/5 — stop hermes-gateway error: %v — %s", err, strings.TrimSpace(string(out)))
	} else {
		log.Printf("[factory-reset/hermes] step 2/5 — hermes-gateway stop returned ok")
	}
	if waitForServiceStop("hermes-gateway", stopVerifyTimeout) {
		log.Printf("[factory-reset/hermes] step 2/5 — hermes-gateway confirmed inactive")
	} else {
		log.Printf("[factory-reset/hermes] step 2/5 — WARNING hermes-gateway still active after %s — SQLite wipe may race the running daemon",
			stopVerifyTimeout)
	}

	// Step 3: hermes setup --reset — resets config.yaml to defaults and scrubs
	// API keys in .env.
	log.Printf("[factory-reset/hermes] step 3/5 — hermes setup --reset --non-interactive")
	if out, err := exec.Command("hermes", "setup", "--reset", "--non-interactive").CombinedOutput(); err != nil {
		log.Printf("[factory-reset/hermes] step 3/5 — hermes setup --reset error: %v — %s", err, strings.TrimSpace(string(out)))
	} else {
		log.Printf("[factory-reset/hermes] step 3/5 — hermes setup --reset done: %s", strings.TrimSpace(string(out)))
	}

	// Step 4: disable hermes-gateway so the service does NOT auto-start on reboot.
	log.Printf("[factory-reset/hermes] step 4/5 — systemctl disable hermes-gateway")
	if out, err := exec.Command("systemctl", "disable", "hermes-gateway").CombinedOutput(); err != nil {
		log.Printf("[factory-reset/hermes] step 4/5 — disable hermes-gateway error: %v — %s", err, strings.TrimSpace(string(out)))
	} else {
		log.Printf("[factory-reset/hermes] step 4/5 — hermes-gateway disabled")
	}

	// Step 5: surgical wipe. Two buckets — enumerated subdirs (rm -rf) and a
	// top-level file sweep keyed by hermesKeepFiles. SQLite DB triples
	// (.db + .db-shm + .db-wal), config.yaml rotating backups, gateway runtime
	// files, etc. are all caught by the sweep — no enumeration needed.
	log.Printf("[factory-reset/hermes] step 5/5 — wiping %d dirs + top-level file sweep", len(hermesWipeDirs))

	for _, d := range hermesWipeDirs {
		osreset.WipePath("[factory-reset/hermes]", d)
	}

	// Top-level file sweep: ReadDir(hermesHome), delete every regular file
	// whose name is NOT in hermesKeepFiles. Dirs at top level are untouched
	// here — they're handled (or intentionally preserved) above.
	entries, err := os.ReadDir(hermesHome)
	if err != nil {
		log.Printf("[factory-reset/hermes] sweep: cannot read %s: %v (non-fatal)", hermesHome, err)
	} else {
		swept := 0
		for _, e := range entries {
			if e.IsDir() {
				continue
			}
			if hermesKeepFiles[e.Name()] {
				continue
			}
			osreset.WipePath("[factory-reset/hermes]", filepath.Join(hermesHome, e.Name()))
			swept++
		}
		log.Printf("[factory-reset/hermes] sweep: removed %d top-level files (keep-list: %d)", swept, len(hermesKeepFiles))
	}

	// SOUL.md: kept by sweep, but its previous content (user-customized
	// persona) must not survive a factory reset. Overwrite with a blank
	// template so the agent starts from "# Hermes Agent Persona" next boot.
	soulPath := filepath.Join(hermesHome, "SOUL.md")
	if err := os.WriteFile(soulPath, []byte(hermesSoulTemplate), 0o644); err != nil {
		log.Printf("[factory-reset/hermes] reset SOUL.md: %v (non-fatal)", err)
	} else {
		log.Printf("[factory-reset/hermes] reset SOUL.md to template")
	}
}
