package skills

import "testing"

func contains(xs []string, want string) bool {
	for _, x := range xs {
		if x == want {
			return true
		}
	}
	return false
}

// A maximal device (every capability) keeps the full catalog.
func TestSupported_MaximalDeviceKeepsAll(t *testing.T) {
	caps := map[string]bool{
		"audio": true, "vision": true, "sensing": true, "presence": true,
		"motion": true, "light": true, "display": true, "expression": true, "media": true,
		"connectivity": true, "companion": true, "system": true,
	}
	if got := Supported(caps); len(got) != len(Catalog) {
		t.Fatalf("maximal device: got %d skills, want full catalog %d", len(got), len(Catalog))
	}
}

// Empty capabilities (DEVICE.md declares none) fails open to the full catalog.
func TestSupported_FailOpen(t *testing.T) {
	if got := Supported(nil); len(got) != len(Catalog) {
		t.Fatalf("nil caps: got %d, want full catalog %d (fail-open)", len(got), len(Catalog))
	}
	if got := Supported(map[string]bool{}); len(got) != len(Catalog) {
		t.Fatalf("empty caps: got %d, want full catalog %d (fail-open)", len(got), len(Catalog))
	}
}

// A reduced device drops only the hardware skills it can't support; platform
// skills (no capability requirement) always survive.
func TestSupported_ReducedDevicePrunesHardware(t *testing.T) {
	// A speaker-only box: audio + sensing, no motion/light/display/vision/presence/media.
	got := Supported(map[string]bool{"audio": true, "sensing": true})

	// People-perception skills (face-enroll, guard, speaker-recognizer,
	// user-emotion-detection) need `presence`, which this box lacks — so they
	// prune even though it has audio/sensing.
	for _, gone := range []string{"servo-control", "servo-tracking", "led-control", "display", "emotion", "scene", "camera", "music", "face-enroll", "guard", "speaker-recognizer", "user-emotion-detection", "computer-use"} {
		if contains(got, gone) {
			t.Errorf("expected %q pruned (device lacks its capability)", gone)
		}
	}
	for _, kept := range []string{"audio", "voice", "sensing", "sensing-track"} {
		if !contains(got, kept) {
			t.Errorf("expected %q kept (audio/sensing satisfied)", kept)
		}
	}
	for _, kept := range []string{"wellbeing", "mood", "habit", "connectors", "music-suggestion", "input-branching"} {
		if !contains(got, kept) {
			t.Errorf("expected platform skill %q kept", kept)
		}
	}
}

// Every capability referenced by the map must be a real DEVICE.md capability key
// (guards against typos drifting the map out of sync with the schema), and every
// mapped skill must exist in the catalog.
func TestCapability_Consistency(t *testing.T) {
	known := map[string]bool{
		"audio": true, "vision": true, "sensing": true, "presence": true,
		"motion": true, "light": true, "display": true, "expression": true, "media": true,
		"connectivity": true, "companion": true, "system": true,
	}
	for skill, cap := range Capability {
		if !known[cap] {
			t.Errorf("skill %q maps to unknown capability %q", skill, cap)
		}
		if !contains(Catalog, skill) {
			t.Errorf("skill %q in Capability map is not in Catalog", skill)
		}
	}
}
