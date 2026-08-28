package http

import (
	"testing"
	"time"
)

// The click mutes turns without aborting them, so a cancelled turn keeps
// reaching tool boundaries and OnToolEnd kept re-arming "one moment" for a
// reply that would never be spoken — device-observed as the lamp promising to
// answer the question the user had just dropped.
func TestCancelAllActiveEndsEveryInFlightRun(t *testing.T) {
	fm := NewFillerManager()
	fm.MarkVoiceRun("device-chat-54-1787885628360")
	fm.OnTurnStart("device-chat-54-1787885628360")
	fm.MarkVoiceRun("device-chat-55-1787885629999")
	fm.OnTurnStart("device-chat-55-1787885629999")

	if n := fm.CancelAllActive(); n != 2 {
		t.Fatalf("expected both in-flight runs cancelled, got %d", n)
	}

	fm.mu.Lock()
	remaining := len(fm.runs)
	fm.mu.Unlock()
	if remaining != 0 {
		t.Errorf("runs map should be empty after cancel-all, got %d", remaining)
	}
}

// A cancelled run must stay dead: the turn is still executing, so tool
// boundaries keep arriving after the click.
func TestToolEndAfterCancelAllDoesNotRearm(t *testing.T) {
	fm := NewFillerManager()
	runID := "device-chat-54-1787885628360"
	fm.MarkVoiceRun(runID)
	fm.OnTurnStart(runID)
	fm.CancelAllActive()

	fm.OnToolEnd(runID)

	fm.mu.Lock()
	_, revived := fm.runs[runID]
	fm.mu.Unlock()
	if revived {
		t.Errorf("tool.end on a cancelled run must not re-register it for fillers")
	}
}

// The Opening filler of whatever the user says NEXT is armed after the click,
// so a run registered afterwards must be unaffected — this is what makes
// "click, then ask something else" still sound normal.
func TestRunStartedAfterCancelAllStillArms(t *testing.T) {
	fm := NewFillerManager()
	fm.MarkVoiceRun("device-chat-54-1787885628360")
	fm.OnTurnStart("device-chat-54-1787885628360")
	fm.CancelAllActive()

	fresh := "device-chat-55-1787885629999"
	fm.MarkVoiceRun(fresh)
	fm.OnTurnStart(fresh)

	fm.mu.Lock()
	run, ok := fm.runs[fresh]
	ended := ok && run.ended
	fm.mu.Unlock()
	if !ok {
		t.Fatalf("a turn started after the click must register for fillers")
	}
	if ended {
		t.Errorf("a turn started after the click must not inherit the cancelled state")
	}
}

func TestCancelAllActiveOnIdleManagerIsANoop(t *testing.T) {
	fm := NewFillerManager()
	if n := fm.CancelAllActive(); n != 0 {
		t.Errorf("expected 0 cancelled on an idle manager, got %d", n)
	}
}

// Guards the iteration-under-lock split in CancelAllActive: the run ids are
// collected while holding fm.mu, then Cancel takes the lock again per run.
func TestCancelAllActiveIsSafeAlongsideConcurrentToolEnds(t *testing.T) {
	fm := NewFillerManager()
	for _, runID := range []string{"run-a", "run-b", "run-c"} {
		fm.MarkVoiceRun(runID)
		fm.OnTurnStart(runID)
	}

	done := make(chan struct{})
	go func() {
		defer close(done)
		for i := 0; i < 200; i++ {
			fm.OnToolEnd("run-b")
		}
	}()
	fm.CancelAllActive()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("concurrent OnToolEnd deadlocked with CancelAllActive")
	}
}
