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
// Edit this slice when new persistent state is introduced anywhere in the
// codebase. Wildcards are NOT expanded — use a directory path and trust the
// recursive remove. Missing paths are silently ignored.
var FactoryResetWipePaths = []string{
	"/root/config",                                  // lamp-server config.json (API keys, channel tokens, MQTT creds)
	"/root/.openclaw",                               // OpenClaw state (sessions, identity, browser profile, memory)
	"/root/local/users",                             // face + voice enrollments (owner)
	"/root/local/strangers",                         // face + voice enrollments (stranger)
	"/var/lib/lelamp/snapshots",                     // persistent camera snapshots (sensing_face / motion / emotion, 72h TTL). Matches lelamp/config.py LELAMP_SNAPSHOT_PERSIST_DIR default.
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

	log.Printf("[factory-reset] starting (wipe %d paths, then reboot)", len(FactoryResetWipePaths))

	go func() {
		// Reset the in-flight flag on any non-reboot exit so a failed run
		// doesn't block subsequent attempts forever. On successful reboot
		// the process dies before this runs — harmless.
		defer func() {
			factoryResetMu.Lock()
			factoryResetInFlight = false
			factoryResetMu.Unlock()
		}()

		// Wipe state. systemd will kill any service holding these files when
		// reboot fires below; no need to stop services first.
		for _, p := range FactoryResetWipePaths {
			if err := os.RemoveAll(p); err != nil {
				log.Printf("[factory-reset] wipe %s failed: %v", p, err)
				// Best-effort — don't abort. Reboot will still proceed and
				// the next boot sees whichever state was successfully wiped.
				continue
			}
			log.Printf("[factory-reset] wiped %s", p)
		}

		// Detached `sleep 2 && systemctl reboot` so the HTTP response makes
		// it out the door and journal flushes before init kills us.
		// Fire-and-forget — we don't Wait() because reboot kills us mid-Wait.
		log.Printf("[factory-reset] rebooting in 2s")
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
