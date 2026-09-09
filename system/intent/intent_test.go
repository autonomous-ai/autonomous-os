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

// indexPhrase underpins tracking target selection, which needs to know WHERE a
// keyword sits, not just whether it is present.
func TestIndexPhrase(t *testing.T) {
	cases := []struct {
		text, kw string
		want     int
	}{
		{"track my keyboard", "keyboard", 9},
		{"let me know", "me", 4},
		{"watch the camera", "me", -1}, // inside "camera"
		{"track the mouse", "us", -1},  // inside "mouse"
		{"lamp previously mentioned", "me", -1},
		{"me first", "me", 0},  // start boundary
		{"follow me", "me", 7}, // end boundary
		{"unmute speaker", "mute speaker", -1},
		{"mute speaker", "mute speaker", 0},
	}
	for _, c := range cases {
		if got := indexPhrase(c.text, c.kw); got != c.want {
			t.Errorf("indexPhrase(%q, %q) = %d, want %d", c.text, c.kw, got, c.want)
		}
	}
}

// Regression: extractTrackTarget walked its keyword table in declaration order
// with a bare strings.Contains, so the pronoun entry ("me"/"us"/"user", table
// position 3) swallowed every request before "keyboard" (position 14) was ever
// tested — and matched inside unrelated words. Captured on green-lamp
// 2026-09-08: three turns asking the lamp to watch a keyboard all fired
// POST /servo/track {"target":["person"]}.
func TestExtractTrackTarget(t *testing.T) {
	cases := map[string]string{
		// The two real device transcripts that carry a tracking verb.
		"so now i am going to type the word angry on my keyboard. you watch me and tell me if i am tapping in the right way.": "keyboard",
		"let me know when you are ready to track my fingers on my keyboard to type for the word. angry.":                      "keyboard",

		// Object noun beats a pronoun regardless of position.
		"watch me type on my keyboard": "keyboard",
		"track my keyboard":            "keyboard",
		"follow the cup":               "cup",

		// Substring collateral that used to resolve to person.
		"track the mouse":  "mouse",
		"watch the camera": "",

		// Pronouns still work when nothing concrete is named.
		"follow me":        "person",
		"track me":         "person",
		"watch the person": "person",
		"follow that guy":  "person",
	}
	for text, want := range cases {
		if got := extractTrackTarget(text); got != want {
			t.Errorf("extractTrackTarget(%q) = %q, want %q", text, got, want)
		}
	}
}

// extractTrackTarget does NOT gate on the verb — hasTrackVerb does, inside the
// rule's match func. So a bare mention of a noun must not fire the rule even
// though the extractor would happily find a target in it.
func TestTrackRuleNeedsAVerb(t *testing.T) {
	for _, text := range []string{
		"yes, i have a keyboard there. so now you are tracking me and see if i type the word angry right.",
		"i have a keyboard here",
	} {
		if r := MatchCommands(text); r != nil {
			t.Errorf("MatchCommands(%q) = %s, want nil", text, r.Rule)
		}
	}
}

func TestTrackVerbEnd(t *testing.T) {
	cases := map[string]bool{
		"track my keyboard":     true,
		"follow me":             true,
		"you watch me type":     true,
		"you are tracking me":   false, // "tracking" is not "track "
		"i can't watch a movie": true,  // verb present; target extraction decides
		"i have a keyboard":     false,
	}
	for text, want := range cases {
		if got := trackVerbEnd(text) >= 0; got != want {
			t.Errorf("trackVerbEnd(%q) >= 0 = %v, want %v", text, got, want)
		}
	}
}
