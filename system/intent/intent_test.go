package intent

import (
	"testing"

	"go.autonomous.ai/os/system/lib/i18n"
)

// Regression: "Deactivate focus mode" used to fall through scene_off (which
// only knew "turn off"/"disable") into scene_focus, re-activating the scene
// the user asked to leave.
func TestSceneOffPhrasings(t *testing.T) {
	for _, text := range []string{
		"Deactivate focus mode",
		"turn off focus mode",
		"disable the focus scene",
		"exit reading mode",
		"stop focus mode",
		"focus mode off",
	} {
		r := Match(text)
		if r == nil || r.Rule != "scene_off" {
			got := "<nil>"
			if r != nil {
				got = r.Rule
			}
			t.Errorf("Match(%q) rule = %s, want scene_off", text, got)
		}
	}
}

func TestSceneActivationStillMatches(t *testing.T) {
	cases := map[string]string{
		"focus mode":           "scene_focus",
		"reading mode please":  "scene_reading",
		"switch to movie mode": "scene_movie",
		"goodnight":            "scene_night",
	}
	for text, want := range cases {
		r := Match(text)
		if r == nil || r.Rule != want {
			got := "<nil>"
			if r != nil {
				got = r.Rule
			}
			t.Errorf("Match(%q) rule = %s, want %s", text, got, want)
		}
	}
}

// Regression: "unmute speaker" used to match the mute_speaker rule because
// anyOf did a bare substring search ("unmute speaker" contains "mute
// speaker") and no unmute rule existed — the device muted on an unmute ask.
func TestMuteUnmuteSpeaker(t *testing.T) {
	cases := map[string]string{
		"unmute speaker":            "unmute_speaker",
		"unmute the speaker please": "unmute_speaker",
		"mute speaker":              "mute_speaker",
		"please mute the speaker":   "mute_speaker",
	}
	for text, want := range cases {
		r := Match(text)
		if r == nil || r.Rule != want {
			got := "<nil>"
			if r != nil {
				got = r.Rule
			}
			t.Errorf("Match(%q) rule = %s, want %s", text, got, want)
		}
	}
}

// Off-phrasings scene_off doesn't recognize must NOT activate a scene —
// falling through to the agent (nil) is the correct behavior.
func TestSceneOffNeverActivates(t *testing.T) {
	for _, text := range []string{
		"kill focus mode",
		"i want out of focus mode",
	} {
		if r := Match(text); r != nil && r.Rule != "scene_off" {
			t.Errorf("Match(%q) rule = %s, must not be a scene activation", text, r.Rule)
		}
	}
}

func TestLocalChitchatAttentionAliasesDoNotDependOnVoiceWakeWordGate(t *testing.T) {
	i18n.SetDeviceName("Moon")
	t.Cleanup(func() { i18n.SetDeviceName("autonomous") })

	cases := map[string]string{
		"moon ơi":            "chitchat_attention",
		"này moon, xin chào": "chitchat_greeting",
	}
	for text, want := range cases {
		r := Match(text)
		if r == nil || r.Rule != want {
			got := "<nil>"
			if r != nil {
				got = r.Rule
			}
			t.Errorf("Match(%q) rule = %s, want %s", text, got, want)
		}
	}
}
