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
	"claude-buddy",
}

// Capability maps a skill to the DEVICE.md capabilities it requires, with
// ANY-OF semantics: a skill is kept when the device declares AT LEAST ONE of the
// listed capabilities. Most skills list a single capability; a skill that can be
// driven by more than one sensor lists each (e.g. user-emotion-detection works
// off a camera OR a mic). A skill absent from this map is a platform/logic skill
// with no hardware dependency and is always installed. The requirement is kept
// here (not in SKILL.md front-matter) so the agentic runtime's skill header stays
// the standard name/description schema and never sees a non-standard key. Keep in
// sync with scripts/provision/setup.sh (skill_cap).
var Capability = map[string][]string{
	"audio":          {device.CapAudio},
	"camera":         {device.CapVision},
	"computer-use":   {device.CapCompanion},
	"display":        {device.CapDisplay},
	"emotion":        {device.CapExpression},
	"led-control":    {device.CapLight},
	"scene":          {device.CapLight},
	"servo-control":  {device.CapMotion},
	"servo-tracking": {device.CapMotion},
	"music":          {device.CapMedia},
	"voice":          {device.CapAudio},
	"sensing":        {device.CapSensing},
	"sensing-track":  {device.CapSensing},
	// People-perception skills understand the PERSON, split by the SENSOR they need:
	//
	//   Camera people-perception (face roster, stranger detection) → `presence`,
	//   the ML people-layer over the camera, NOT the raw `vision` sensor: a camera
	//   that only streams (vision, no presence) must not load face-enroll/guard.
	//
	//   Voice people-perception (who is speaking) → `audio`, i.e. the mic — a mic
	//   is all speaker-ID needs, no camera and no presence people-layer. So any
	//   device with a mic gets it; not a hard requirement (Supported fails open and
	//   skips it when audio is absent).
	//
	//   user-emotion-detection is ONE skill over TWO triggers — facial `[emotion]`
	//   (camera, gated `presence`) and vocal `[speech_emotion]` (mic, gated
	//   `audio`). It loads when the device has EITHER sensor: a mic-only device
	//   (intern-v2) keeps the voice branch, a camera-only device keeps the face
	//   branch, and a device with both (lamp) gets both.
	//
	// Keep in sync with the HAL gates: voice people-perception on `audio`
	// (server.py VoiceService), camera people-perception on `presence`
	// (server.py SensingService).
	"face-enroll":            {device.CapPresence},
	"guard":                  {device.CapPresence},
	"speaker-recognizer":     {device.CapAudio},
	"user-emotion-detection": {device.CapAudio, device.CapPresence},
	// Voice-approve Claude tool prompts from the Mac companion → needs a mic+speaker
	// to ask the user out loud. Harmless on audio devices without the companion
	// daemon (the sensing event simply never fires).
	"claude-buddy": {device.CapAudio},
}

// Supported filters the catalog to the skills a device with deviceCaps can run:
// a skill is kept when it requires no capability (platform skill) or the device
// declares AT LEAST ONE of the skill's required capabilities (any-of). Fail-open:
// empty deviceCaps → full catalog (a device that declares no capabilities keeps
// everything, matching legacy behavior). The maximal reference device (Lamp)
// declares every capability, so it keeps all.
func Supported(deviceCaps map[string]bool) []string {
	if len(deviceCaps) == 0 {
		return Catalog
	}
	out := make([]string, 0, len(Catalog))
	for _, name := range Catalog {
		reqs := Capability[name]
		if len(reqs) == 0 || hasAny(deviceCaps, reqs) {
			out = append(out, name)
		}
	}
	return out
}

// hasAny reports whether deviceCaps declares at least one of reqs.
func hasAny(deviceCaps map[string]bool, reqs []string) bool {
	for _, c := range reqs {
		if deviceCaps[c] {
			return true
		}
	}
	return false
}
