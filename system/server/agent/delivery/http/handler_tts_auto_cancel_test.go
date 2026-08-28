package http

import (
	"testing"
	"time"

	sensinghttp "go.autonomous.ai/os/system/server/sensing/delivery/http"
)

// The realtime agent answers the newest question, so the main-agent turn still
// working on the previous one must not speak its answer afterwards.
func TestRealtimeHandledMutesOlderInFlightTurn(t *testing.T) {
	t.Setenv("OS_REALTIME_AUTO_MUTE", "1")
	h := newCancelTestHandler()
	older := deviceRunID(5, time.Now().Add(-2*time.Second))

	h.CancelSpeechForNewerTurn()

	if !h.isSpeechCancelled(older) {
		t.Fatalf("turn in flight when realtime answered a newer utterance must lose the speaker")
	}
}

// The system-stamped mark takes the speaker and nothing else. A turn whose body
// the user really did ask for must still run — silently dropping it would read
// as the device ignoring the request.
func TestRealtimeHandledDoesNotDropHardware(t *testing.T) {
	t.Setenv("OS_REALTIME_AUTO_MUTE", "1")
	h := newCancelTestHandler()
	older := deviceRunID(5, time.Now().Add(-2*time.Second))

	h.CancelSpeechForNewerTurn()

	if h.isHWCancelled(older) {
		t.Errorf("auto mute must not drop servo/LED markers — only the physical click goes that far")
	}
}

// Fillers follow the speech, not the hardware. Leaving them armed reproduced
// the very thing the click had to fix: the device answers the new question in
// the realtime voice, then promises "one moment" about the old one and falls
// silent. A filler is not an action the user asked for.
func TestRealtimeHandledDropsPendingFillers(t *testing.T) {
	t.Setenv("OS_REALTIME_AUTO_MUTE", "1")
	h := newCancelTestHandler()
	fm := sensinghttp.DefaultFillerManager
	runID := "device-chat-54-1787885628360"
	fm.MarkVoiceRun(runID)
	fm.OnTurnStart(runID)
	t.Cleanup(func() { fm.Cancel(runID) })

	h.CancelSpeechForNewerTurn()

	fm.OnToolEnd(runID)
	if fm.HasActiveRun(runID) {
		t.Errorf("the muted turn must not keep re-arming fillers for an answer it can no longer speak")
	}
}

// ...but the switch still governs it: not opted in means nothing changes.
func TestAutoMuteOffLeavesFillersAlone(t *testing.T) {
	h := newCancelTestHandler()
	fm := sensinghttp.DefaultFillerManager
	runID := "device-chat-55-1787885629999"
	fm.MarkVoiceRun(runID)
	fm.OnTurnStart(runID)
	t.Cleanup(func() { fm.Cancel(runID) })

	h.CancelSpeechForNewerTurn()

	if !fm.HasActiveRun(runID) {
		t.Errorf("without OS_REALTIME_AUTO_MUTE=1 filler state must be untouched")
	}
}

// The physical click keeps its stronger meaning now that a second mark exists.
func TestPhysicalClickStillDropsHardware(t *testing.T) {
	h := newCancelTestHandler()
	older := deviceRunID(5, time.Now().Add(-2*time.Second))

	h.CancelSpeech()

	if !h.isHWCancelled(older) {
		t.Errorf("click must still drop the body of an in-flight turn")
	}
}

// "Realtime answers, then the user asks the main agent something new" — the new
// turn is on the far side of the mark and speaks. This is why the mark is a
// timestamp and not a suppressed flag.
func TestTurnStartedAfterRealtimeHandledStillSpeaks(t *testing.T) {
	t.Setenv("OS_REALTIME_AUTO_MUTE", "1")
	h := newCancelTestHandler()

	h.CancelSpeechForNewerTurn()

	newer := deviceRunID(6, time.Now().Add(2*time.Second))
	if h.isSpeechCancelled(newer) {
		t.Errorf("a turn created after the realtime answer must speak")
	}
}

// The two marks are independent: the auto mark must still mute a turn the click
// was too early to catch, and must not widen the click's hardware verdict.
func TestMarksDoNotOverwriteEachOther(t *testing.T) {
	t.Setenv("OS_REALTIME_AUTO_MUTE", "1")
	h := newCancelTestHandler()
	h.CancelSpeech()
	// Strictly between the two marks: after the click, before the realtime answer.
	time.Sleep(2 * time.Millisecond)
	betweenTurn := deviceRunID(7, time.Now())
	time.Sleep(2 * time.Millisecond)
	h.CancelSpeechForNewerTurn()

	if !h.isSpeechCancelled(betweenTurn) {
		t.Errorf("turn created after the click but before the realtime answer must be muted by the auto mark")
	}
	if h.isHWCancelled(betweenTurn) {
		t.Errorf("that turn started after the click, so its body must still run")
	}
}

// Off by default: a body that has never heard of this switch keeps the old
// behaviour — realtime answers, the older turn still speaks.
func TestAutoMuteIsOffUnlessOptedIn(t *testing.T) {
	h := newCancelTestHandler()
	older := deviceRunID(5, time.Now().Add(-2*time.Second))

	h.CancelSpeechForNewerTurn()

	if h.isSpeechCancelled(older) {
		t.Errorf("without OS_REALTIME_AUTO_MUTE=1 the in-flight turn must stay audible")
	}
}
