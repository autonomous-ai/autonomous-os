package system

import (
	"fmt"
	"log"
	"net/http"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/lib/osreset"
	"go.autonomous.ai/os/server/serializers"
)

var deviceWipePaths = []string{
	"/root/config/config.json",                      // os-server config (API keys, channel tokens, MQTT creds) — bootstrap.json in the same dir is intentionally kept
	"/root/config/agent_state.json",                 // agent-runtime switch history/baseline — MUST wipe in lockstep with config.json (which holds agent_runtime). Leaving it makes prev (stale, e.g. hermes) diverge from the reset current (default) and triggers a spurious persona migration on next boot that propagates the just-wiped stub persona across runtimes.
	"/root/local/users",                             // face + voice enrollments (owner)
	"/root/local/strangers",                         // face + voice enrollments (stranger)
	"/var/lib/hal/snapshots",                        // persistent camera snapshots (sensing_face / motion / emotion, 72h TTL)
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

// runFactoryReset is the trigger-agnostic worker. Returns immediately after
// spawning the wipe + reboot goroutine; callers (HTTP / MQTT / GPIO) decide
// how to surface acceptance to the user.
//
// Returns (started, errStatus, errMessage). errStatus mirrors HTTP semantics
// so HTTP callers can use it directly; non-HTTP callers (MQTT/GPIO) just
// check started=false and log errMessage.
func runFactoryReset(gw domain.AgentGateway) (started bool, errStatus int, errMessage string) {
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

	// The runtime to reset is whatever backend is currently running: factory reset
	// calls gw.ResetAgent() on the active gateway, so there is no per-backend switch
	// to keep in sync (adding a backend = implement ResetAgent, nothing here). A
	// runtime whose state is owned externally (e.g. PicoClaw) ships a no-op
	// ResetAgent, so it is correctly left untouched.
	log.Printf("[factory-reset] accepted — resetting active agent → wipe %d device paths → reboot",
		len(deviceWipePaths))

	go func() {
		defer func() {
			factoryResetMu.Lock()
			factoryResetInFlight = false
			factoryResetMu.Unlock()
		}()

		if err := gw.ResetAgent(); err != nil {
			log.Printf("[factory-reset] agent reset error: %v (continuing with device wipe)", err)
		}

		wipeDeviceState()

		// Detached reboot so the HTTP response escapes before init kills us.
		log.Printf("[factory-reset] all done — rebooting in 2s")
		if err := exec.Command("sh", "-c", "(sleep 2 && systemctl reboot) &").Start(); err != nil {
			log.Printf("[factory-reset] schedule reboot failed: %v", err)
		}
	}()

	return true, 0, ""
}

// wipeDeviceState removes per-device state independent of the agent backend
func wipeDeviceState() {
	log.Printf("[factory-reset] wiping %d device paths", len(deviceWipePaths))
	for _, p := range deviceWipePaths {
		osreset.WipePath("[factory-reset]", p)
	}
}

// FactoryReset performs a soft factory reset: wipe device state (config / API
// keys / enrollments / WiFi creds) + reboot. Kernel / OS / system packages /
// binaries / hal .venv are NOT touched — this is a state reset, not a
// reflash. After reboot the device boots into AP "<device_type>-XXXX" with a fresh
// setup wizard.
//
// POST /api/system/factory-reset
//
// No body. The currently-active agent backend is reset via gw.ResetAgent() (a
// backend whose state is owned externally ships a no-op ResetAgent).
//
// For per-component binary refresh use POST /api/system/software-update/:target.
//
// Returns 202 Accepted with the work scheduled in the background — the
// goroutine reboots the device, so the response must be sent before reboot
// fires. 409 Conflict if another reset is already running; 429 Too Many
// Requests inside the cooldown window.
func FactoryReset(c *gin.Context, gw domain.AgentGateway) {
	// Audit who triggered this destructive action. Logged BEFORE runFactoryReset
	// so even a rejected attempt (cooldown / single-flight / failed auth) leaves a
	// trail. RemoteAddr is the TCP peer (always 127.0.0.1 for nginx-proxied
	// requests); X-Forwarded-For / X-Real-IP carry the real client behind nginx.
	// A pure-loopback caller (no XFF/X-Real-IP) bypassed auth — i.e. an on-device
	// process (the GPIO button handler, the agent, a local curl, or localhost web
	// UI). The Authorization scheme (never the token itself) + os_session cookie
	// presence distinguish a remote admin Bearer call from a logged-in web UI.
	authScheme := ""
	if h := c.GetHeader("Authorization"); h != "" {
		if i := strings.IndexByte(h, ' '); i > 0 {
			authScheme = h[:i] // e.g. "Bearer" — token deliberately not logged
		} else {
			authScheme = "present"
		}
	}
	_, cookieErr := c.Cookie("os_session")
	log.Printf("[factory-reset] TRIGGER received — remote=%s xff=%q x-real-ip=%q user-agent=%q auth=%q session-cookie=%v",
		c.Request.RemoteAddr,
		c.GetHeader("X-Forwarded-For"),
		c.GetHeader("X-Real-IP"),
		c.Request.UserAgent(),
		authScheme,
		cookieErr == nil,
	)

	started, status, msg := runFactoryReset(gw)
	if !started {
		if status == http.StatusTooManyRequests {
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
		"started":      true,
		"message":      "Soft factory reset started. Device will wipe its state and reboot into AP setup mode (~30s).",
		"device_wipes": deviceWipePaths,
	}))
}

// TriggerFactoryReset is the entry point for non-HTTP triggers (MQTT command
// handler, GPIO long-press service). Returns whether the trigger was accepted
// (single-flight + cooldown gates apply identically). Caller logs the outcome.
func TriggerFactoryReset(gw domain.AgentGateway) (started bool, reason string) {
	started, _, msg := runFactoryReset(gw)
	return started, msg
}
