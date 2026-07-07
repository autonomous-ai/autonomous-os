package codex

import (
	"log"
	"os"
	"os/exec"
	"strings"
	"time"

	"go.autonomous.ai/os/lib/osreset"
)

// Paths + unit set up by install.sh (see install.sh).
const (
	codexDataDir           = "/root/.codex" // data dir (HOME=/root)
	codexUnit              = "codex"        // systemd unit name
	codexStopVerifyTimeout = 5 * time.Second
)

// ResetAgent is the Codex factory-reset wipe, called on the active gateway by
// server/system/factoryreset.go. Codex keeps nothing worth preserving:
// config.toml/.env are regenerated from the project config.json by presync.sh
// on the next switch, and wiping /root/.codex also drops CLI auth, threads
// (sessions/) and the migrate marker — a true factory state.
func (s *CodexService) ResetAgent() error {
	wipeCodexState()
	return nil
}

// wipeCodexState: stop+disable gateway → wipe /root/.codex → recreate baseline dirs.
func wipeCodexState() {
	// 1. Stop the gateway (Restart=always is overridden by an explicit stop) and
	//    confirm it is down — it holds the data dir open, so it must die before wipe.
	log.Printf("[factory-reset/codex] step 1/4 — systemctl stop codex")
	if out, err := exec.Command("systemctl", "stop", codexUnit).CombinedOutput(); err != nil {
		log.Printf("[factory-reset/codex] step 1/4 — stop error: %v — %s", err, strings.TrimSpace(string(out)))
	}
	if waitForCodexStop(codexUnit, codexStopVerifyTimeout) {
		log.Printf("[factory-reset/codex] step 1/4 — confirmed inactive")
	} else {
		log.Printf("[factory-reset/codex] step 1/4 — WARNING still active after %s", codexStopVerifyTimeout)
	}

	// 2. Disable — reboot defaults to openclaw; switch-runtime re-enables on switch back.
	log.Printf("[factory-reset/codex] step 2/4 — systemctl disable codex")
	if out, err := exec.Command("systemctl", "disable", codexUnit).CombinedOutput(); err != nil {
		log.Printf("[factory-reset/codex] step 2/4 — disable error: %v — %s", err, strings.TrimSpace(string(out)))
	}

	// 3. Wipe everything under /root/.codex: config.toml, .env (gatewayd bridge
	//    env), auth.json (CLI auth), sessions/, workspace/, attachments/,
	//    install.log and the .openclaw-migrated marker.
	log.Printf("[factory-reset/codex] step 3/4 — wiping %s", codexDataDir)
	osreset.WipePath("[factory-reset/codex]", codexDataDir)

	// 4. Recreate the baseline dirs (Codex has no onboard subcommand — presync
	//    re-asserts config.toml/.env on the next switch; the CLI recreates its
	//    own state under CODEX_HOME on first run). Non-fatal.
	log.Printf("[factory-reset/codex] step 4/4 — recreate baseline dirs")
	for _, d := range []string{codexDataDir + "/workspace", codexDataDir + "/attachments"} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			log.Printf("[factory-reset/codex] step 4/4 — mkdir %s error: %v (non-fatal)", d, err)
		}
	}
}

// waitForCodexStop polls is-active until the unit is inactive or timeout elapses.
func waitForCodexStop(unit string, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for {
		if exec.Command("systemctl", "is-active", "--quiet", unit).Run() != nil {
			return true // non-zero exit → not active
		}
		if time.Now().After(deadline) {
			return false
		}
		time.Sleep(200 * time.Millisecond)
	}
}
