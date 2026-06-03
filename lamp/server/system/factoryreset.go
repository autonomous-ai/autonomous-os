package system

import (
	"fmt"
	"log"
	"net/http"
	"os"
	"os/exec"
	"strconv"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	"go-lamp.autonomous.ai/server/serializers"
)

// FactoryResetWipePaths lists everything the soft reset deletes. These are the
// per-device files written by the setup wizard + runtime — wiping them returns
// the device to "fresh out of box" state. Binaries, systemd units, lelamp .venv,
// kernel, and OS packages are NOT touched (those belong to software-update).
//
// Kept intentionally (openclaw starts normally after reboot without re-onboard):
//   - /root/.openclaw/openclaw.json      — process config
//   - /root/.openclaw/npm/               — installed plugins
//   - /root/.openclaw/plugin-skills/     — skill definitions
//   - /root/.openclaw/canvas/            — UI registry
//   - /root/.openclaw/plugins/           — plugin registry
//   - /root/.openclaw/identity/          — device identity keypair
//   - /root/.openclaw/lumi-device-key.json
//
// Edit this slice when new persistent state is introduced. Missing paths are
// silently ignored.
var FactoryResetWipePaths = []string{
	"/root/config",                            // lamp-server config.json (API keys, channel tokens, MQTT creds)
	"/root/.openclaw/agents",                  // conversation sessions + history
	"/root/.openclaw/workspace",               // agent memory (HEARTBEAT.md, SOUL.md, USER.md, memory/)
	"/root/.openclaw/devices",                 // paired devices list
	"/root/.openclaw/credentials",             // channel auth tokens (telegram, discord, slack sessions)
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
	"/root/.openclaw/openclaw.json.last-good", // stale config backup written on clean shutdown
	"/root/.openclaw/update-check.json",       // OTA update-check timestamp
	"/root/local/users",                       // face + voice enrollments (owner)
	"/root/local/strangers",                   // face + voice enrollments (stranger)
	"/var/lib/lelamp/snapshots",               // persistent camera snapshots
	"/etc/wpa_supplicant/wpa_supplicant-wlan0.conf", // home WiFi credentials → forces AP mode on next boot
}

// FactoryResetMinInterval is the minimum gap between two factory-reset
// triggers. Acts as a circuit breaker against runaway callers and accidental
// double-clicks.
const FactoryResetMinInterval = 5 * time.Minute

// Single-flight + cooldown state shared across all trigger surfaces (HTTP /
// MQTT / GPIO). Package-level globals are fine — this is a singleton operation
// per device, no second instance should ever run.
var (
	factoryResetMu       sync.Mutex
	factoryResetInFlight bool
	factoryResetLastFire time.Time
)

// FactoryResetOptions captures caller-supplied params. Currently empty — soft
// reset takes no inputs — but kept as a named type so call sites stay stable
// when future knobs (confirm_token, force, etc.) get added.
type FactoryResetOptions struct{}

// runFactoryReset is the trigger-agnostic worker. Returns immediately after
// spawning the wipe + reboot goroutine; callers (HTTP / MQTT / GPIO) decide
// how to surface acceptance to the user.
//
// Returns (started, errStatus, errMessage). errStatus mirrors HTTP semantics
// so HTTP callers can use it directly; non-HTTP callers (MQTT/GPIO) just
// check started=false and log errMessage.
func runFactoryReset(opts FactoryResetOptions) (started bool, errStatus int, errMessage string) {
	factoryResetMu.Lock()
	if factoryResetInFlight {
		factoryResetMu.Unlock()
		return false, http.StatusConflict, "factory-reset already running"
	}
	if !factoryResetLastFire.IsZero() {
		if wait := FactoryResetMinInterval - time.Since(factoryResetLastFire); wait > 0 {
			factoryResetMu.Unlock()
			return false, http.StatusTooManyRequests,
				fmt.Sprintf("factory-reset rate-limited, retry in %ds", int(wait.Seconds())+1)
		}
	}
	factoryResetInFlight = true
	factoryResetLastFire = time.Now()
	factoryResetMu.Unlock()

	log.Printf("[factory-reset] accepted — stopping openclaw, wiping %d paths, then reboot", len(FactoryResetWipePaths))

	go func() {
		defer func() {
			factoryResetMu.Lock()
			factoryResetInFlight = false
			factoryResetMu.Unlock()
		}()

		// Step 1: stop openclaw cleanly before wiping so it can't recreate
		// state dirs during the window between wipe and reboot.
		log.Printf("[factory-reset] step 1/3 — stopping openclaw service")
		if out, err := exec.Command("systemctl", "stop", "openclaw").CombinedOutput(); err != nil {
			log.Printf("[factory-reset] stop openclaw: %v — %s (non-fatal, continuing)", err, string(out))
		} else {
			log.Printf("[factory-reset] openclaw stopped")
		}

		// Step 2: wipe all state paths.
		log.Printf("[factory-reset] step 2/3 — wiping %d paths", len(FactoryResetWipePaths))
		for _, p := range FactoryResetWipePaths {
			if err := os.RemoveAll(p); err != nil {
				log.Printf("[factory-reset] wipe %s: %v (non-fatal)", p, err)
				continue
			}
			log.Printf("[factory-reset] wiped %s", p)
		}

		// Step 3: reboot. Detached so the HTTP response escapes before init kills us.
		log.Printf("[factory-reset] step 3/3 — rebooting in 2s")
		if err := exec.Command("sh", "-c", "(sleep 2 && systemctl reboot) &").Start(); err != nil {
			log.Printf("[factory-reset] schedule reboot failed: %v", err)
		}
	}()

	return true, 0, ""
}

// FactoryReset performs a soft factory reset: wipe Lamp state (config / API
// keys / enrollments / WiFi creds) + reboot. Kernel / OS / system packages /
// binaries / lelamp .venv are NOT touched — this is a state reset, not a
// reflash. After reboot the device boots into AP "Lamp-XXXX" with a fresh
// setup wizard.
//
// POST /api/system/factory-reset   (body ignored)
//
// For per-component binary refresh use POST /api/system/software-update/:target.
//
// Returns 202 Accepted with the work scheduled in the background — the
// goroutine reboots the device, so the response must be sent before reboot
// fires. 409 Conflict if another reset is already running; 429 Too Many
// Requests inside the cooldown window.
func FactoryReset(c *gin.Context) {
	var opts FactoryResetOptions
	_ = c.ShouldBindJSON(&opts) // body is optional; empty body is fine

	started, status, msg := runFactoryReset(opts)
	if !started {
		if status == http.StatusTooManyRequests {
			// Surface Retry-After so the web UI can show a useful countdown.
			factoryResetMu.Lock()
			wait := FactoryResetMinInterval - time.Since(factoryResetLastFire)
			factoryResetMu.Unlock()
			if wait > 0 {
				c.Header("Retry-After", strconv.Itoa(int(wait.Seconds())+1))
			}
		}
		c.JSON(status, serializers.ResponseError(msg))
		return
	}

	c.JSON(http.StatusAccepted, serializers.ResponseSuccess(gin.H{
		"started": true,
		"message": "Soft factory reset started. Device will wipe Lamp state and reboot into AP setup mode (~30s).",
		"wipes":   FactoryResetWipePaths,
	}))
}

// TriggerFactoryReset is the entry point for non-HTTP triggers (MQTT command
// handler, GPIO long-press service). Returns whether the trigger was accepted
// (single-flight + cooldown gates apply identically). Caller logs the outcome.
func TriggerFactoryReset(opts FactoryResetOptions) (started bool, reason string) {
	started, _, msg := runFactoryReset(opts)
	return started, msg
}
