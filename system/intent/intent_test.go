package intent

import (
	"strings"
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

// TestChitchatWholeWordOnly locks the fix for a substring match that answered
// ordinary sentences as greetings: "hi" sits inside "this", "his", "machine",
// so "What is this?" was replied to with "Hi there!" locally and never reached
// the agent.
func TestChitchatWholeWordOnly(t *testing.T) {
	SetChitchatEnabled(true)
	t.Cleanup(func() { SetChitchatEnabled(true) })

	for _, text := range []string{
		"Body of his arm.",
		"What is this?",
		"This is broken",
		"His name is Tom",
		"The machine is loud",
	} {
		if r := Match(text); r != nil && strings.HasPrefix(r.Rule, "chitchat_") {
			t.Errorf("Match(%q) = %s, want no chitchat match", text, r.Rule)
		}
	}

	for _, text := range []string{"hi", "hello there", "hey", "bye", "thanks a lot"} {
		r := Match(text)
		if r == nil || !strings.HasPrefix(r.Rule, "chitchat_") {
			t.Errorf("Match(%q) = %v, want a chitchat match", text, r)
		}
	}
}

// TestChitchatDisabled covers the realtime case: the voice agent answers social
// talk itself, so os-server must not also fire a canned reply — while command
// intents keep working.
func TestChitchatDisabled(t *testing.T) {
	SetChitchatEnabled(false)
	t.Cleanup(func() { SetChitchatEnabled(true) })

	if r := Match("hi"); r != nil {
		t.Errorf("Match(\"hi\") = %s, want nil when chitchat is off", r.Rule)
	}
	if r := Match("turn on the light"); r == nil || r.Rule != "led_on" {
		t.Errorf("Match(\"turn on the light\") = %v, want led_on", r)
	}
}
