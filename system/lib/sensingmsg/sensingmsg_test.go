package sensingmsg

import (
	"strings"
	"testing"
)

func TestBuildVoiceFollowupIsAnAuthorizedUserTurn(t *testing.T) {
	got := Build("voice_followup", "play music", "", "")
	if !strings.HasPrefix(got, "[user] play music") {
		t.Fatalf("voice_followup = %q, want user-priority message", got)
	}
	if strings.Contains(got, "[ambient]") {
		t.Fatalf("voice_followup must not be marked ambient: %q", got)
	}
}

func TestBuildPresenceEnterCarriesCurrentUser(t *testing.T) {
	got := Build("presence.enter", "Person detected — 1 face(s) visible (friend (long))", "long", "")
	if !strings.Contains(got, "[context: current_user=long]") {
		t.Fatalf("presence.enter = %q, want current_user attribution", got)
	}
}

func TestBuildPresenceEnterUnknownUserIsLabelledUnknown(t *testing.T) {
	// A stranger (or a face that has not resolved yet) must still carry the
	// tag — greeting routes key "speak no name" off current_user=unknown, so
	// an absent tag would be read as "no constraint" and let the agent fall
	// back to the persona name.
	got := Build("presence.enter", "Person detected — 1 face(s) visible (stranger (stranger_3))", "", "")
	if !strings.Contains(got, "[context: current_user=unknown]") {
		t.Fatalf("presence.enter with no user = %q, want current_user=unknown", got)
	}
}
