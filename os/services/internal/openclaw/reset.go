package openclaw

import (
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"go.autonomous.ai/os/lib/osreset"
)

// ResetAgent performs the OpenClaw factory-reset wipe. The factory-reset flow
// (server/system/factoryreset.go) resolves the active gateway and calls this on
// it — so adding a backend means implementing ResetAgent, not editing a switch.
func (s *OpenclawService) ResetAgent() error {
	wipeOpenclawState()
	return nil
}

// openclawStatePaths are openclaw runtime state dirs wiped on factory reset.
// openclaw reset --scope config+creds+sessions removes openclaw.json +
// credentials but not these dirs — wipe them manually.
// Missing paths are silently ignored.
var openclawStatePaths = []string{
	"/root/.openclaw/agents",                  // conversation sessions + history
	"/root/.openclaw/workspace",               // agent memory (HEARTBEAT.md, SOUL.md, USER.md, memory/)
	"/root/.openclaw/devices",                 // paired devices list
	"/root/.openclaw/tasks",                   // background task runs
	"/root/.openclaw/logs",                    // runtime logs
	"/root/.openclaw/telegram",                // telegram update offset
	"/root/.openclaw/discord",                 // discord command deploy cache
	"/root/.openclaw/plugin-state",            // plugin runtime state
	"/root/.openclaw/memory",                  // memory sqlite db
	"/root/.openclaw/delivery-queue",          // failed message delivery queue
	"/root/.openclaw/subagents",               // subagent run history
	"/root/.openclaw/cron",                    // cron jobs + state
	"/root/.openclaw/media",                   // outbound media files
	"/root/.openclaw/flows",                   // flow registry
	"/root/.openclaw/openclaw.json.last-good", // stale config backup
	// openclaw.json.bak* handled by glob below — covers .bak, .bak.1, .bak.2 … .bak.n
	"/root/.openclaw/update-check.json", // OTA update-check timestamp
	"/root/.openclaw/.openclaw",         // nested stale workspace from initial install
	"/root/.openclaw/.cache",            // runtime cache (preventive)
	// Kept by openclaw reset --scope config+creds+sessions:
	//   npm/, plugin-skills/, canvas/, plugins/, identity/, device-key.json
	// openclaw.json is intentionally wiped and NOT restored — SetupAgent detects
	// the missing file and calls onboardOpenclaw() to create a fresh one.
	// openclaw.service is disabled before reboot so it is NOT running when
	// onboard executes (onboard fails if the gateway is already up).
}

// wipeOpenclawState runs the 3-step OpenClaw reset: CLI reset → disable service
// → manual rm -rf of dirs the CLI doesn't touch.
func wipeOpenclawState() {
	// Step 1: openclaw reset — stops the gateway cleanly, wipes openclaw.json
	// + credentials. Preserves npm/, plugin-skills/, identity/.
	// openclaw.json is intentionally NOT restored: SetupAgent will detect it
	// missing and call onboardOpenclaw() → fresh config on next setup.
	log.Printf("[factory-reset/openclaw] step 1/3 — openclaw reset --scope config+creds+sessions")
	out, err := exec.Command("openclaw", "reset",
		"--scope", "config+creds+sessions",
		"--yes", "--non-interactive",
	).CombinedOutput()
	outStr := strings.TrimSpace(string(out))
	if err != nil {
		log.Printf("[factory-reset/openclaw] step 1/3 — openclaw reset error: %v — %s", err, outStr)
	} else {
		log.Printf("[factory-reset/openclaw] step 1/3 — openclaw reset done: %s", outStr)
	}

	// Step 2: disable openclaw.service so it does NOT auto-start on reboot.
	// Without this, the service starts without openclaw.json (broken state)
	// and onboardOpenclaw() fails with "gateway already running".
	// SetupAgent re-enables it via restartOpenclawGateway() after onboard.
	log.Printf("[factory-reset/openclaw] step 2/3 — disabling openclaw.service")
	if out, err := exec.Command("systemctl", "disable", "openclaw").CombinedOutput(); err != nil {
		log.Printf("[factory-reset/openclaw] step 2/3 — disable openclaw error: %v — %s", err, strings.TrimSpace(string(out)))
	} else {
		log.Printf("[factory-reset/openclaw] step 2/3 — openclaw.service disabled")
	}

	// Step 3: wipe remaining state. First glob-wipe all openclaw.json.bak* variants
	// (.bak, .bak.1, .bak.2 … .bak.n) to prevent credential leaks regardless of
	// how many rotating backups openclaw made.
	if bakFiles, err := filepath.Glob("/root/.openclaw/openclaw.json.bak*"); err == nil {
		for _, f := range bakFiles {
			if err := os.Remove(f); err != nil {
				if os.IsNotExist(err) {
					continue
				}
				log.Printf("[factory-reset/openclaw] wipe %s: %v (non-fatal)", f, err)
				continue
			}
			log.Printf("[factory-reset/openclaw] wiped %s", f)
		}
	}
	log.Printf("[factory-reset/openclaw] step 3/3 — wiping %d openclaw state paths", len(openclawStatePaths))
	for _, p := range openclawStatePaths {
		osreset.WipePath("[factory-reset/openclaw]", p)
	}
}
