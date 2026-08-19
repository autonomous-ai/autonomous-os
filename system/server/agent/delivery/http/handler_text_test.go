package http

import "testing"

func TestIsMetaNonReply(t *testing.T) {
	suppressed := []string{
		"Sound event, no user message. Nothing to say",
		"Nothing to say.",
		"No reply needed.",
		"No response necessary",
		"No need to respond here.",
		"Staying silent.",
		"  no user message  ",
	}
	for _, s := range suppressed {
		if !isMetaNonReply(s) {
			t.Errorf("isMetaNonReply(%q) = false, want true", s)
		}
	}

	spoken := []string{
		"",
		"That noise startled me!",
		"Do you need anything?",
		"Is there nothing to say about that?", // question → real reply
		"I have nothing to say about the weather today, but I did notice the light dropped a lot since this morning and the room is getting cold.", // long → real reply
	}
	for _, s := range spoken {
		if isMetaNonReply(s) {
			t.Errorf("isMetaNonReply(%q) = true, want false", s)
		}
	}
}
