package domain

import (
	"strings"
	"testing"
	"time"
)

func TestAppendEnrollNudgeIsConditionalAndPreservesTranscript(t *testing.T) {
	lastEnrollNudgeMu.Lock()
	previous := lastEnrollNudge
	lastEnrollNudge = time.Time{}
	lastEnrollNudgeMu.Unlock()
	t.Cleanup(func() {
		lastEnrollNudgeMu.Lock()
		lastEnrollNudge = previous
		lastEnrollNudgeMu.Unlock()
	})

	plain := "Speaker - Alex: Review this code."
	if got := AppendEnrollNudge(plain); got != plain {
		t.Fatalf("recognized speaker changed: %q", got)
	}
	msg := "Unknown Speaker: [voice:voice_475] Let's discuss the review of the Codex agent code for autonomous OS. (audio save at /tmp/voice_475/turn.wav)"
	got := AppendEnrollNudge(msg)
	if !strings.HasPrefix(got, msg) || strings.Contains(got, "[REQUIRED:") {
		t.Fatalf("transcript changed or unconditional skill activation: %q", got)
	}
	if !strings.Contains(got, "only for a clear self-introduction") || !strings.Contains(got, "Otherwise handle the request") {
		t.Fatalf("missing enrollment entry gate: %q", got)
	}
	if repeated := AppendEnrollNudge(msg); repeated != msg {
		t.Fatalf("cooldown did not preserve original message: %q", repeated)
	}
}
