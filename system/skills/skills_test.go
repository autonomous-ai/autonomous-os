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
		"connectivity": true, "companion": true, "system": true, "environment": true,
	}
	if got := Supported(caps); len(got) != len(Catalog) {
		t.Fatalf("maximal device: got %d skills, want full catalog %d", len(got), len(Catalog))
	}
}

// Empty capabilities preserve legacy skills without opting into environment.
func TestSupported_FailOpenPreservesLegacyOnly(t *testing.T) {
	for _, caps := range []map[string]bool{nil, {}} {
		got := Supported(caps)
		for _, name := range Catalog {
			if name == "environment" {
				if contains(got, name) {
					t.Error("missing capabilities must not install environment")
				}
			} else if !contains(got, name) {
				t.Errorf("legacy fail-open must preserve %q", name)
			}
		}
		if len(got) != len(Catalog)-1 {
			t.Errorf("got %d skills, want %d legacy skills", len(got), len(Catalog)-1)
		}
	}
}

// A reduced device drops only the hardware skills it can't support; platform
// skills (no capability requirement) always survive.
func TestSupported_ReducedDevicePrunesHardware(t *testing.T) {
	// A speaker-only box (like intern-v2): audio + sensing, no
	// motion/light/display/vision/presence/media.
	got := Supported(map[string]bool{"audio": true, "sensing": true})

	// Camera people-perception (face-enroll, guard) needs `presence`, which this
	// box lacks — so they prune. Voice people-perception (speaker-recognizer,
	// user-emotion-detection) gates on `audio` (the mic), which this box HAS — so
	// they survive (see the kept list below).
	for _, gone := range []string{"servo-control", "servo-tracking", "led-control", "display", "emotion", "scene", "camera", "music", "face-enroll", "guard", "computer-use", "environment"} {
		if contains(got, gone) {
			t.Errorf("expected %q pruned (device lacks its capability)", gone)
		}
	}
	for _, kept := range []string{"audio", "voice", "sensing", "sensing-track", "speaker-recognizer", "user-emotion-detection"} {
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

// Every capability referenced by the map must be a real ROBOT.md capability key
// (guards against typos drifting the map out of sync with the schema), and every
// mapped skill must exist in the catalog.
func TestCapability_Consistency(t *testing.T) {
	known := map[string]bool{
		"audio": true, "vision": true, "sensing": true, "presence": true,
		"motion": true, "light": true, "display": true, "expression": true, "media": true,
		"connectivity": true, "companion": true, "system": true, "environment": true,
	}
	for skill, caps := range Capability {
		if len(caps) == 0 {
			t.Errorf("skill %q maps to an empty capability list (drop it from the map to mark it a platform skill)", skill)
		}
		for _, cap := range caps {
			if !known[cap] {
				t.Errorf("skill %q maps to unknown capability %q", skill, cap)
			}
		}
		if !contains(Catalog, skill) {
			t.Errorf("skill %q in Capability map is not in Catalog", skill)
		}
	}
}

// user-emotion-detection is one skill over two sensors (face + voice). It must
// survive on a device with EITHER the mic (audio) or the camera people-layer
// (presence) — and only fully prune when the device has neither.
func TestSupported_UserEmotionDetectionAnyOfSensor(t *testing.T) {
	cases := []struct {
		name string
		caps map[string]bool
		want bool
	}{
		{"mic-only (intern-v2): voice branch", map[string]bool{"audio": true, "sensing": true}, true},
		{"camera-only: face branch", map[string]bool{"vision": true, "presence": true}, true},
		{"both (lamp)", map[string]bool{"audio": true, "presence": true}, true},
		{"neither sensor", map[string]bool{"light": true, "system": true}, false},
	}
	for _, tc := range cases {
		got := contains(Supported(tc.caps), "user-emotion-detection")
		if got != tc.want {
			t.Errorf("%s: user-emotion-detection present=%v, want %v", tc.name, got, tc.want)
		}
		// speaker-recognizer is voice-only: present iff the device has a mic.
		wantSpeaker := tc.caps["audio"]
		if gotSpeaker := contains(Supported(tc.caps), "speaker-recognizer"); gotSpeaker != wantSpeaker {
			t.Errorf("%s: speaker-recognizer present=%v, want %v (audio=%v)", tc.name, gotSpeaker, wantSpeaker, tc.caps["audio"])
		}
	}
}

// Environmental sensing does not imply a camera or an expression actuator.
func TestSupported_EnvironmentOnly(t *testing.T) {
	got := Supported(map[string]bool{"environment": true})
	for _, kept := range []string{"environment", "wellbeing"} {
		if !contains(got, kept) {
			t.Errorf("expected %q on an environment-only device", kept)
		}
	}
	for _, gone := range []string{"camera", "emotion", "sensing", "guard"} {
		if contains(got, gone) {
			t.Errorf("environment capability must not install %q", gone)
		}
	}
	if contains(Supported(map[string]bool{"environment": false}), "environment") {
		t.Error("explicit false environment capability must not install environment skill")
	}
}
