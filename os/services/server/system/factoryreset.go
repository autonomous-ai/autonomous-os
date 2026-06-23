package system

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/server/serializers"
)

const configFilePath = "/root/config/config.json"

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

// FactoryResetOptions captures caller-supplied params.
// Backend overrides the auto-detected agent backend (read from config.json
// agent_runtime). Accepts "openclaw" | "hermes" | "" (auto). Unknown values
// fall back to openclaw — matches the runtime selector in agent/factory.go.
type FactoryResetOptions struct {
	Backend string `json:"backend,omitempty"`
}

// readAgentRuntime returns the agent_runtime field from /root/config/config.json,
// or "" if the file is missing/unreadable/malformed.
func readAgentRuntime() string {
	data, err := os.ReadFile(configFilePath)
	if err != nil {
		return ""
	}
	var c struct {
		AgentRuntime string `json:"agent_runtime"`
	}
	if err := json.Unmarshal(data, &c); err != nil {
		return ""
	}
	return strings.ToLower(strings.TrimSpace(c.AgentRuntime))
}

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

	// Detect active backend BEFORE we wipe config.json. Explicit opts.Backend (from HTTP body) wins over auto-detect; both fall back to openclaw.
	backend := strings.ToLower(strings.TrimSpace(opts.Backend))
	source := "request"
	if backend == "" {
		backend = readAgentRuntime()
		source = "config"
	}
	if backend != "hermes" && backend != "openclaw" {
		log.Printf("[factory-reset] backend resolution: source=%s value=%q → falling back to openclaw", source, backend)
		backend = "openclaw"
	} else {
		log.Printf("[factory-reset] backend resolution: source=%s value=%q", source, backend)
	}

	log.Printf("[factory-reset] accepted — backend=%s → reset → wipe %d device paths → reboot",
		backend, len(deviceWipePaths))

	go func() {
		defer func() {
			factoryResetMu.Lock()
			factoryResetInFlight = false
			factoryResetMu.Unlock()
		}()

		switch backend {
		case "hermes":
			wipeHermesState()
		default:
			wipeOpenclawState()
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
		wipePath("[factory-reset]", p)
	}
}

func wipePath(prefix, p string) {
	if _, err := os.Stat(p); os.IsNotExist(err) {
		return
	}
	if err := os.RemoveAll(p); err != nil {
		if os.IsNotExist(err) {
			return
		}
		log.Printf("%s wipe %s: %v (non-fatal)", prefix, p, err)
		return
	}
	log.Printf("%s wiped %s", prefix, p)
}

// FactoryReset performs a soft factory reset: wipe device state (config / API
// keys / enrollments / WiFi creds) + reboot. Kernel / OS / system packages /
// binaries / hal .venv are NOT touched — this is a state reset, not a
// reflash. After reboot the device boots into AP "<device_type>-XXXX" with a fresh
// setup wizard.
//
// POST /api/system/factory-reset
//
// Body (optional): {"backend": "openclaw" | "hermes"} to override the
// auto-detected backend. Empty body = auto-detect from config.json
// agent_runtime field.
//
// For per-component binary refresh use POST /api/system/software-update/:target.
//
// Returns 202 Accepted with the work scheduled in the background — the
// goroutine reboots the device, so the response must be sent before reboot
// fires. 409 Conflict if another reset is already running; 429 Too Many
// Requests inside the cooldown window.
func FactoryReset(c *gin.Context) {
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

	var opts FactoryResetOptions
	_ = c.ShouldBindJSON(&opts) // body is optional; empty body is fine

	started, status, msg := runFactoryReset(opts)
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
func TriggerFactoryReset(opts FactoryResetOptions) (started bool, reason string) {
	started, _, msg := runFactoryReset(opts)
	return started, msg
}
