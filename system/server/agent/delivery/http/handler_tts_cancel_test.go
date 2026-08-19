package http

import (
	"fmt"
	"testing"
	"time"
)

func newCancelTestHandler() *AgentHandler {
	return &AgentHandler{runFirstSeenMs: make(map[string]int64)}
}

func deviceRunID(seq int, at time.Time) string {
	return fmt.Sprintf("device-chat-%d-%d", seq, at.UnixMilli())
}

// The scenario the feature exists for: several turns are already in flight,
// the user clicks, then immediately says something new. Every older turn must
// lose the speaker; the new one must keep it.
func TestCancelSpeechMutesBacklogButNotNewTurns(t *testing.T) {
	h := newCancelTestHandler()
	now := time.Now()

	backlog := make([]string, 0, 8)
	for i := 0; i < 8; i++ {
		backlog = append(backlog, deviceRunID(i, now.Add(-time.Duration(i+1)*time.Second)))
	}

	for _, runID := range backlog {
		if h.isSpeechCancelled(runID) {
			t.Fatalf("run %s muted before any cancel", runID)
		}
	}

	h.CancelSpeech()

	for _, runID := range backlog {
		if !h.isSpeechCancelled(runID) {
			t.Errorf("in-flight run %s should be muted after cancel", runID)
		}
	}

	fresh := deviceRunID(99, time.Now().Add(time.Millisecond))
	if h.isSpeechCancelled(fresh) {
		t.Errorf("run created after the cancel must still speak, got muted: %s", fresh)
	}
}

// A second click must mute the turns started since the first one, so holding
// the watermark monotone is not enough on its own — it has to move forward.
func TestCancelSpeechWatermarkAdvances(t *testing.T) {
	h := newCancelTestHandler()

	h.CancelSpeech()
	mid := deviceRunID(1, time.Now().Add(time.Millisecond))
	if h.isSpeechCancelled(mid) {
		t.Fatalf("run after first cancel should speak")
	}

	time.Sleep(2 * time.Millisecond)
	h.CancelSpeech()
	if !h.isSpeechCancelled(mid) {
		t.Errorf("second cancel must also mute the turn started after the first")
	}
}

// Channel runs carry no timestamp. A sequence-numbered id must never be read
// as a 1970 date — that would mute every Telegram turn forever.
func TestChannelRunIDsUseFirstSeenNotSequence(t *testing.T) {
	h := newCancelTestHandler()

	talking := "tg-session-42" // already speaking when the click lands
	h.runCreatedAtMs(talking)  // observed before the cancel

	time.Sleep(2 * time.Millisecond)
	h.CancelSpeech()
	time.Sleep(2 * time.Millisecond)

	if !h.isSpeechCancelled(talking) {
		t.Errorf("channel turn already speaking at cancel time should be muted")
	}
	if h.isSpeechCancelled("tg-session-43") {
		t.Errorf("channel turn first heard after the cancel should still speak")
	}
}

// An empty runID has no turn to attribute the speech to; muting it would take
// the speaker away from OS-level notices that pass through the same path.
func TestCancelSpeechIgnoresEmptyRunID(t *testing.T) {
	h := newCancelTestHandler()
	h.CancelSpeech()
	if h.isSpeechCancelled("") {
		t.Errorf("empty runID must not be treated as cancelled")
	}
}

func TestNoCancelMeansNothingMuted(t *testing.T) {
	h := newCancelTestHandler()
	if h.isSpeechCancelled(deviceRunID(1, time.Now().Add(-time.Hour))) {
		t.Errorf("without a cancel even an old run must speak")
	}
}
