// Package skills holds the platform skill catalog and the hardware capability
// each skill requires. This is OS-level metadata, independent of which agentic
// runtime (OpenClaw, Hermes, or any other) actually loads the skills into its
// workspace — so it lives here, not in a runtime package. Runtimes consume
// Catalog + Supported to decide which skills to provision for a given device.
package skills

import "go.autonomous.ai/os/internal/device"

// Catalog is the full set of skill names the OS publishes (mirrors the skills/
// tree and what scripts/release/upload-skills.sh pushes to the CDN).
var Catalog = []string{
	"audio",
	"camera",
	"computer-use",
	"connectors",
	"display",
	"emotion",
	"face-enroll",
	"guard",
	"led-control",
	"music",
	"music-suggestion",
	"scene",
	"sensing",
	"sensing-track",
	"servo-control",
	"servo-tracking",
	"voice",
	"wellbeing",
	"mood",
	"speaker-recognizer",
	"user-emotion-detection",
	"habit",
	"input-branching",
}

// Capability maps a skill to the DEVICE.md capability it requires. A skill
// absent from this map is a platform/logic skill with no hardware dependency and
// is always installed. The requirement is kept here (not in SKILL.md
// front-matter) so the agentic runtime's skill header stays the standard
// name/description schema and never sees a non-standard key. Keep in sync with
// scripts/provision/setup.sh (skill_cap).
var Capability = map[string]string{
	"audio":          device.CapAudio,
	"camera":         device.CapVision,
	"computer-use":   device.CapCompanion,
	"display":        device.CapDisplay,
	"emotion":        device.CapExpression,
	"led-control":    device.CapLight,
	"scene":          device.CapLight,
	"servo-control":  device.CapMotion,
	"servo-tracking": device.CapMotion,
	"music":          device.CapMedia,
	"voice":          device.CapAudio,
	"sensing":        device.CapSensing,
	"sensing-track":  device.CapSensing,
	// People-perception skills — they understand the PERSON (face roster, stranger
	// detection, who is speaking, the user's mood). That is the `presence`
	// capability (the ML people-layer over camera/mic), NOT the raw sensor: a
	// camera that only streams (vision, no presence) must not load face-enroll, and
	// a device that doesn't perceive people must not get user-emotion-detection,
	// or the skill loads with no perception events to act on. Keep in sync with the
	// presence-gated perception loop in os/hal sensing/voice.
	"face-enroll":            device.CapPresence,
	"guard":                  device.CapPresence,
	"speaker-recognizer":     device.CapPresence,
	"user-emotion-detection": device.CapPresence,
}

// Supported filters the catalog to the skills a device with deviceCaps can run:
// a skill is kept when it requires no capability (platform skill) or the device
// declares that capability. Fail-open: empty deviceCaps → full catalog (a device
// that declares no capabilities keeps everything, matching legacy behavior). The
// maximal reference device (Lamp) declares every capability, so it keeps all.
func Supported(deviceCaps map[string]bool) []string {
	if len(deviceCaps) == 0 {
		return Catalog
	}
	out := make([]string, 0, len(Catalog))
	for _, name := range Catalog {
		if cap := Capability[name]; cap == "" || deviceCaps[cap] {
			out = append(out, name)
		}
	}
	return out
}
