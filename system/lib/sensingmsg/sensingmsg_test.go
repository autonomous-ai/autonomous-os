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
	got := Build("presence.enter", "Person detected — new: friend (long); faces in frame: 1 (long)", "long", "")
	if !strings.Contains(got, "[context: current_user=long]") {
		t.Fatalf("presence.enter = %q, want current_user attribution", got)
	}
}

func TestBuildPresenceEnterUnknownUserIsLabelledUnknown(t *testing.T) {
	// A stranger (or a face that has not resolved yet) must still carry the
	// tag — greeting routes key "speak no name" off current_user=unknown, so
	// an absent tag would be read as "no constraint" and let the agent fall
	// back to the persona name.
	got := Build("presence.enter", "Person detected — new: stranger (stranger_3); faces in frame: 1 (stranger_3)", "", "")
	if !strings.Contains(got, "[context: current_user=unknown]") {
		t.Fatalf("presence.enter with no user = %q, want current_user=unknown", got)
	}
}

func TestEnvironmentUsesDedicatedSkill(t *testing.T) {
	got := Build("environment.update", "PM2.5 changed", "", "")
	if !strings.HasPrefix(got, "[environment:update] PM2.5 changed") || !strings.Contains(got, "No mandatory speech or emotion") {
		t.Fatalf("environment routing = %q", got)
	}
	if strings.Contains(got, "[sensing:") || strings.Contains(got, "[guard-active]") {
		t.Fatalf("unexpected sensing/guard routing: %q", got)
	}
}

func TestBuildPresenceEnterNewFriendCarriesPresenceContext(t *testing.T) {
	got := Build("presence.enter", "Person detected — new: friend (long); faces in frame: 1 (long)", "long", "")
	if !strings.Contains(got, "[presence_context:") {
		t.Fatalf("friend presence.enter = %q, want presence_context block", got)
	}
}

func TestBuildPresenceEnterStrangerJoiningPresentFriendSkipsPresenceContext(t *testing.T) {
	// The friend is still current_user (inside her forget window) but the
	// arrival is a stranger. The block describes HER last leave, which the
	// agent read as "Long re-entering after ~28 min away" and greeted the
	// wrong situation (orange-lamp, 2026-09-16). Only a NEW friend gets it.
	got := Build("presence.enter",
		"Person detected — new: stranger (stranger_1); already present: long (friend); faces in frame: 2 (long, stranger_1)",
		"long", "")
	if strings.Contains(got, "[presence_context:") {
		t.Fatalf("stranger-only presence.enter = %q, must not carry presence_context", got)
	}
	if !strings.Contains(got, "[context: current_user=long]") {
		t.Fatalf("stranger-only presence.enter = %q, attribution tag must stay", got)
	}
}

func TestBuildPresenceEnterLoneStrangerInsideFriendWindowSkipsPresenceContext(t *testing.T) {
	// Same current_user, friend out of frame: the block would be equally wrong.
	got := Build("presence.enter", "Person detected — new: stranger (stranger_4); faces in frame: 1 (stranger_4)", "long", "")
	if strings.Contains(got, "[presence_context:") {
		t.Fatalf("lone-stranger presence.enter = %q, must not carry presence_context", got)
	}
}
