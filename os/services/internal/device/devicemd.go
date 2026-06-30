package device

import (
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

var (
	reFrontMatter     = regexp.MustCompile(`(?s)^---\s*\n(.*?)\n---\s*\n`)
	reGatewayBlock    = regexp.MustCompile(`(?m)^gateway:[ \t]*\n((?:[ \t]+.*\n?)+)`)
	reGatewayDefault  = regexp.MustCompile(`(?m)^[ \t]+default:[ \t]*(\S+)`)
	reGatewayProtocol = regexp.MustCompile(`(?m)^[ \t]+protocol:[ \t]*(\S+)`)
	reSoulRef         = regexp.MustCompile(`(?m)^soul_ref:[ \t]*(\S+)`)
	reCapBlock        = regexp.MustCompile(`(?m)^capabilities:[ \t]*\n((?:[ \t]+.*\n?)+)`)
	reCapKey          = regexp.MustCompile(`(?m)^[ \t]+(\w+):`)
	reStartupVolume   = regexp.MustCompile(`(?m)^startup_volume:[ \t]*(\d+)`)
)

// DefaultStartupVolume is the speaker volume os-server sets at startup when a
// device's DEVICE.md does not declare `startup_volume`. 100% keeps the legacy
// behavior — software at max so the hardware/alsactl level is the effective
// control — for any device that hasn't opted into a per-device level.
const DefaultStartupVolume = 100

// Capabilities returns the set of capability keys declared in the
// `capabilities:` block of devices/<deviceType>/DEVICE.md (e.g. audio, vision,
// motion, light, display, …), or nil if absent/unreadable. Dependency-free
// front-matter parse, mirroring SoulRef/GatewayDefault. The capability keys are
// what gate which hardware/body skills a device loads (see openclaw onboarding):
// a skill that declares `capability: motion` is only shipped to a device whose
// DEVICE.md declares `motion`.
func Capabilities(deviceType string) map[string]bool {
	b, err := os.ReadFile(filepath.Join(DevicesDir(), deviceType, "DEVICE.md"))
	if err != nil {
		return nil
	}
	fm := reFrontMatter.FindSubmatch(b)
	if fm == nil {
		return nil
	}
	blk := reCapBlock.FindSubmatch(fm[1])
	if blk == nil {
		return nil
	}
	caps := map[string]bool{}
	for _, m := range reCapKey.FindAllSubmatch(blk[1], -1) {
		caps[strings.TrimSpace(string(m[1]))] = true
	}
	return caps
}

// Capability names — the frozen capability vocabulary (capabilities.v1) from
// contract/capabilities.md. This is the platform-wide feature taxonomy every
// DEVICE.md declares against and every OS-core gate asks about; it is NOT a
// device-specific value. Defined once here (the package that parses the
// capabilities block) so the strings have a single source of truth: a typo is a
// compile error, not a silent fail-open. Keep in sync with contract/
// capabilities.md and the skills.Capability / skills.HookCapability maps.
const (
	CapAudio    = "audio"
	CapVision   = "vision"
	CapSensing  = "sensing"
	CapPresence = "presence"
	CapMotion   = "motion"
	CapLight    = "light"
	CapDisplay  = "display"
	// CapExpression — the body can show emotion (the /emotion route). An output
	// capability declared when the device has a screen, LED, or servo to express
	// through; the route degrades to whatever output is present. Distinct from the
	// perception capabilities (vision/sensing/presence). See contract/capabilities.md.
	CapExpression   = "expression"
	CapMedia        = "media"
	CapConnectivity = "connectivity"
	CapCompanion    = "companion"
	CapSystem       = "system"
)

// RouteCapability maps a HAL hardware-route path to the capability it requires,
// or "" when the path has no hardware dependency (os-server routes like /speak,
// /broadcast, /wellbeing, or always-present audio paths). It lets the os-server
// drop an agent's [HW:/path:...] marker for hardware the body doesn't have —
// the OS, not the swappable brain, is the deterministic gate over the body. Keep
// the route→capability mapping aligned with the DEVICE.md route declarations and
// skills.Capability. Conservative: unmapped paths fail open (the POST proceeds,
// HAL handles it), so only the clearly expressive/motor routes are gated here.
func RouteCapability(path string) string {
	switch {
	case strings.HasPrefix(path, "/emotion"):
		return CapExpression
	case strings.HasPrefix(path, "/scene"), strings.HasPrefix(path, "/led"):
		return CapLight
	case strings.HasPrefix(path, "/servo"):
		return CapMotion
	case strings.HasPrefix(path, "/display"):
		return CapDisplay
	case strings.HasPrefix(path, "/music"):
		return CapMedia
	default:
		return ""
	}
}

// Has reports whether deviceType declares the given capability in its DEVICE.md.
// Fail-open: a device whose capabilities block is absent/unreadable → true,
// preserving legacy single-device behavior (the maximal reference device, Lamp,
// declares every capability anyway). Use it to gate OS-core calls to OPTIONAL
// hardware (servo/camera/display) so a device that lacks the capability is never
// driven against hardware it doesn't have — e.g. intern-v2 (audio+light only)
// must not be told to move a servo or polled for a display it has no concept of.
func Has(deviceType, capability string) bool {
	caps := Capabilities(deviceType)
	if len(caps) == 0 {
		return true
	}
	return caps[capability]
}

// SoulRef returns the `soul_ref` declared in devices/<deviceType>/DEVICE.md, or
// "" if absent/unreadable. The value is either a path (read relative to the
// device dir) or an http(s) URL (downloaded) — see openclaw.deviceSoulCore.
// Dependency-free front-matter parse, mirroring GatewayDefault.
func SoulRef(deviceType string) string {
	b, err := os.ReadFile(filepath.Join(DevicesDir(), deviceType, "DEVICE.md"))
	if err != nil {
		return ""
	}
	fm := reFrontMatter.FindSubmatch(b)
	if fm == nil {
		return ""
	}
	m := reSoulRef.FindSubmatch(fm[1])
	if m == nil {
		return ""
	}
	return strings.TrimSpace(string(m[1]))
}

// StartupVolume returns the `startup_volume` (0-100) declared in
// devices/<deviceType>/DEVICE.md, or DefaultStartupVolume (100) when the field
// is absent/unreadable/out of range. os-server applies this once at startup so
// the boot speaker level is a per-device body property, not a hardcoded max —
// e.g. a device with a loud speaker can boot quieter. Dependency-free
// front-matter parse, mirroring SoulRef. Fail-safe to max, never to silent.
func StartupVolume(deviceType string) int {
	b, err := os.ReadFile(filepath.Join(DevicesDir(), deviceType, "DEVICE.md"))
	if err != nil {
		return DefaultStartupVolume
	}
	fm := reFrontMatter.FindSubmatch(b)
	if fm == nil {
		return DefaultStartupVolume
	}
	m := reStartupVolume.FindSubmatch(fm[1])
	if m == nil {
		return DefaultStartupVolume
	}
	v, err := strconv.Atoi(strings.TrimSpace(string(m[1])))
	if err != nil || v < 0 || v > 100 {
		return DefaultStartupVolume
	}
	return v
}

// DevicesDir resolves the per-device profile root (devices/<type>/...).
// DEVICES_DIR env wins; falls back to /opt/devices (mirrors HAL + onboarding.go).
func DevicesDir() string {
	if d := os.Getenv("DEVICES_DIR"); d != "" {
		return d
	}
	return "/opt/devices"
}

// gatewayField extracts one sub-field (matched by re) from the `gateway:` block
// of devices/<deviceType>/DEVICE.md, or "" if absent/unreadable. Dependency-free
// front-matter parse (no YAML lib), mirroring hal/board/device.py.
func gatewayField(deviceType string, re *regexp.Regexp) string {
	b, err := os.ReadFile(filepath.Join(DevicesDir(), deviceType, "DEVICE.md"))
	if err != nil {
		return ""
	}
	fm := reFrontMatter.FindSubmatch(b)
	if fm == nil {
		return ""
	}
	blk := reGatewayBlock.FindSubmatch(fm[1])
	if blk == nil {
		return ""
	}
	m := re.FindSubmatch(blk[1])
	if m == nil {
		return ""
	}
	return strings.TrimSpace(string(m[1]))
}

// GatewayDefault returns the `gateway.default` (agentic runtime) declared in
// devices/<deviceType>/DEVICE.md, or "" if absent.
func GatewayDefault(deviceType string) string {
	return gatewayField(deviceType, reGatewayDefault)
}

// GatewayProtocol returns the `gateway.protocol` (wire transport) declared in
// devices/<deviceType>/DEVICE.md, or "" if absent. The transport is actually a
// property of the runtime (openclaw→websocket, hermes→sse), so this is consumed
// only as a consistency guard — see agent.ProvideGateway.
func GatewayProtocol(deviceType string) string {
	return gatewayField(deviceType, reGatewayProtocol)
}
