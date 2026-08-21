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
	reVoiceBlock      = regexp.MustCompile(`(?m)^voice:[ \t]*\n((?:[ \t]+.*\n?)+)`)
	reVoiceProvider   = regexp.MustCompile(`(?m)^[ \t]+tts_provider:[ \t]*(\S+)`)
	reVoiceVoice      = regexp.MustCompile(`(?m)^[ \t]+tts_voice:[ \t]*(\S+)`)
	reVoiceWakeWord   = regexp.MustCompile(`(?m)^[ \t]+wakeword:[ \t]*(\S+)`)
	reSoulRef         = regexp.MustCompile(`(?m)^soul_ref:[ \t]*(\S+)`)
	reCapBlock        = regexp.MustCompile(`(?m)^capabilities:[ \t]*\n((?:[ \t]+.*\n?)+)`)
	reCapKey          = regexp.MustCompile(`(?m)^[ \t]+(\w+):`)
	reStartupVolume   = regexp.MustCompile(`(?m)^startup_volume:[ \t]*(\d+)`)
)

// DefaultStartupVolume is the speaker volume os-server sets at startup when a
// device's ROBOT.md does not declare `startup_volume`. 100% keeps the legacy
// behavior — software at max so the hardware/alsactl level is the effective
// control — for any device that hasn't opted into a per-device level.
const DefaultStartupVolume = 100

// readFrontMatter returns the front-matter block of
// robots/<deviceType>/ROBOT.md, or nil when the file or the block is
// absent/unreadable. Dependency-free parse (no YAML lib), mirroring
// hal/board/device.py — every ROBOT.md field accessor below goes through it.
func readFrontMatter(deviceType string) []byte {
	// ROBOT.md is canonical; DEVICE.md is the name it shipped under and stays
	// readable, because robots in the field have it on disk.
	dir := filepath.Join(DevicesDir(), deviceType)
	b, err := os.ReadFile(filepath.Join(dir, "ROBOT.md"))
	if err != nil {
		b, err = os.ReadFile(filepath.Join(dir, "DEVICE.md"))
	}
	if err != nil {
		return nil
	}
	fm := reFrontMatter.FindSubmatch(b)
	if fm == nil {
		return nil
	}
	return fm[1]
}

// Capabilities returns the set of capability keys declared in the
// `capabilities:` block of robots/<deviceType>/ROBOT.md (e.g. audio, vision,
// motion, light, display, …), or nil if absent/unreadable. The capability keys are
// what gate which hardware/body skills a device loads (see openclaw onboarding):
// a skill that declares `capability: motion` is only shipped to a device whose
// ROBOT.md declares `motion`.
func Capabilities(deviceType string) map[string]bool {
	fm := readFrontMatter(deviceType)
	if fm == nil {
		return nil
	}
	blk := reCapBlock.FindSubmatch(fm)
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
// robots/contract/capabilities.md. This is the platform-wide feature taxonomy every
// ROBOT.md declares against and every OS-core gate asks about; it is NOT a
// device-specific value. Defined once here (the package that parses the
// capabilities block) so the strings have a single source of truth: a typo is a
// compile error, not a silent fail-open. Keep in sync with robots/contract/
// capabilities.md and the skills.Capability / skills.HookCapability maps.
const (
	CapAudio    = "audio"
	CapVision   = "vision"
	CapSensing  = "sensing"
	CapPresence = "presence"
	CapMotion   = "motion"
	CapPolicy   = "policy"
	CapLight    = "light"
	CapDisplay  = "display"
	// CapExpression — the body can show emotion (the /emotion route). An output
	// capability declared when the device has a screen, LED, or servo to express
	// through; the route degrades to whatever output is present. Distinct from the
	// perception capabilities (vision/sensing/presence). See robots/contract/capabilities.md.
	CapExpression = "expression"
	// CapLifelike — the body opts into the idle "living creature" suite
	// (breathing LED, servo micro-movements, TTS self-talk) run by
	// system/ambient. Routeless (the OUT-side mirror of routeless presence):
	// it is a personality opt-in, not a route. Each idle loop additionally
	// requires the matching hardware capability (light/motion/audio); a body
	// that declares the hardware but not lifelike stays quiet when idle.
	CapLifelike     = "lifelike"
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
// the route→capability mapping aligned with the ROBOT.md route declarations and
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
	case strings.HasPrefix(path, "/policy"):
		return CapPolicy
	case strings.HasPrefix(path, "/display"):
		return CapDisplay
	case strings.HasPrefix(path, "/music"):
		return CapMedia
	default:
		return ""
	}
}

// Has reports whether deviceType declares the given capability in its ROBOT.md.
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

// SoulRef returns the `soul_ref` declared in robots/<deviceType>/ROBOT.md, or
// "" if absent/unreadable. The value is either a path (read relative to the
// device dir) or an http(s) URL (downloaded) — see openclaw.deviceSoulCore.
func SoulRef(deviceType string) string {
	fm := readFrontMatter(deviceType)
	if fm == nil {
		return ""
	}
	m := reSoulRef.FindSubmatch(fm)
	if m == nil {
		return ""
	}
	return strings.TrimSpace(string(m[1]))
}

// StartupVolume returns the `startup_volume` (0-100) declared in
// robots/<deviceType>/ROBOT.md, or DefaultStartupVolume (100) when the field
// is absent/unreadable/out of range. os-server applies this once at startup so
// the boot speaker level is a per-device body property, not a hardcoded max —
// e.g. a device with a loud speaker can boot quieter. Fail-safe to max, never
// to silent.
func StartupVolume(deviceType string) int {
	fm := readFrontMatter(deviceType)
	if fm == nil {
		return DefaultStartupVolume
	}
	m := reStartupVolume.FindSubmatch(fm)
	if m == nil {
		return DefaultStartupVolume
	}
	v, err := strconv.Atoi(strings.TrimSpace(string(m[1])))
	if err != nil || v < 0 || v > 100 {
		return DefaultStartupVolume
	}
	return v
}

// DevicesDir resolves the per-device profile root (robots/<type>/...).
// DEVICES_DIR env wins; falls back to /opt/devices (mirrors HAL + onboarding.go).
func DevicesDir() string {
	if d := os.Getenv("DEVICES_DIR"); d != "" {
		return d
	}
	return "/opt/devices"
}

// gatewayField extracts one sub-field (matched by re) from the `gateway:` block
// of robots/<deviceType>/ROBOT.md, or "" if absent/unreadable.
func gatewayField(deviceType string, re *regexp.Regexp) string {
	return blockField(deviceType, reGatewayBlock, re)
}

// blockField extracts one sub-field (matched by fieldRe) from a top-level
// front-matter block (matched by blockRe) of robots/<deviceType>/ROBOT.md,
// or "" if absent/unreadable. Shared by the gateway: and voice: accessors.
func blockField(deviceType string, blockRe, fieldRe *regexp.Regexp) string {
	fm := readFrontMatter(deviceType)
	if fm == nil {
		return ""
	}
	blk := blockRe.FindSubmatch(fm)
	if blk == nil {
		return ""
	}
	m := fieldRe.FindSubmatch(blk[1])
	if m == nil {
		return ""
	}
	return strings.TrimSpace(string(m[1]))
}

// GatewayDefault returns the `gateway.default` (agentic runtime) declared in
// robots/<deviceType>/ROBOT.md, or "" if absent.
func GatewayDefault(deviceType string) string {
	return gatewayField(deviceType, reGatewayDefault)
}

// GatewayProtocol returns the `gateway.protocol` (wire transport) declared in
// robots/<deviceType>/ROBOT.md, or "" if absent. The transport is actually a
// property of the runtime (openclaw→websocket, hermes→sse), so this is consumed
// only as a consistency guard — see agent.ProvideGateway.
func GatewayProtocol(deviceType string) string {
	return gatewayField(deviceType, reGatewayProtocol)
}

// voiceField extracts one sub-field (matched by re) from the `voice:` block of
// robots/<deviceType>/ROBOT.md, or "" if absent/unreadable.
func voiceField(deviceType string, re *regexp.Regexp) string {
	return blockField(deviceType, reVoiceBlock, re)
}

// TTSProvider returns the `voice.tts_provider` declared in
// robots/<deviceType>/ROBOT.md, or "" if absent/unreadable. It is the device's
// DEFAULT TTS provider (openai | elevenlabs) — a body property, since some
// devices ship with a voice that only sounds right on a particular provider.
// os-server seeds config.json's tts_provider from this once at startup when the
// user hasn't chosen one, so the Setup UI, HAL auto-start, and StartHALVoice all
// see the same default; the user can still override it in Setup/Settings. No
// declaration → "" → the legacy default (openai) is kept.
func TTSProvider(deviceType string) string {
	return voiceField(deviceType, reVoiceProvider)
}

// TTSVoice returns the `voice.tts_voice` declared in
// robots/<deviceType>/ROBOT.md, or "" if absent/unreadable. Optional companion
// to TTSProvider: pins the device's default voice explicitly (e.g. a provider
// voice name like "Ngan", or a raw provider voice id). When absent, os-server
// falls back to a language-aware default for the seeded provider (see
// domain.DefaultElevenLabsVoiceForLang). Accepted verbatim — the device author
// owns its validity, mirroring how startup_volume trusts its number.
func TTSVoice(deviceType string) string {
	return voiceField(deviceType, reVoiceVoice)
}

// WakeWordDefault returns the `voice.wakeword` declared in
// robots/<deviceType>/ROBOT.md and whether it was declared at all. It is the
// body's OUT-OF-THE-BOX gate: a lamp sitting in a shared room wants to be
// addressed before it answers, while a push-to-talk body does not.
//
// os-server adopts it only while config.json carries no wakeword key — i.e. a
// config it just created. A device provisioned before the switch existed is
// pinned to false by ProvideConfig, so an OTA can never make a unit already in
// use stop answering. Undeclared or unparseable → (false, false) → the legacy
// always-listening default.
func WakeWordDefault(deviceType string) (value bool, declared bool) {
	switch strings.ToLower(voiceField(deviceType, reVoiceWakeWord)) {
	case "true":
		return true, true
	case "false":
		return false, true
	default:
		return false, false
	}
}
