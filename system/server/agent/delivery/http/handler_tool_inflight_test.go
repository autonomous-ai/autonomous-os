package http

import (
	"sort"
	"testing"
	"time"

	sensinghttp "go.autonomous.ai/os/system/server/sensing/delivery/http"
)

// A tool that started and has not ended is what makes a turn "mid-tool".
func TestToolStartOpensAndEndCloses(t *testing.T) {
	h := newCancelTestHandler()
	h.noteToolStart("device-chat-5-1787885628360", "call-1")

	if got := h.runsWithOpenTool(); len(got) != 1 || got[0] != "device-chat-5-1787885628360" {
		t.Fatalf("open tool must be reported, got %v", got)
	}
	h.noteToolEnd("device-chat-5-1787885628360", "call-1")
	if got := h.runsWithOpenTool(); len(got) != 0 {
		t.Fatalf("ended tool must not be reported, got %v", got)
	}
}

// Parallel tool calls in one turn: the run stays open until the LAST one ends.
func TestRunStaysOpenUntilEveryToolEnds(t *testing.T) {
	h := newCancelTestHandler()
	run := "device-chat-6-1787885628360"
	h.noteToolStart(run, "a")
	h.noteToolStart(run, "b")
	h.noteToolEnd(run, "a")

	if got := h.runsWithOpenTool(); len(got) != 1 {
		t.Fatalf("one tool still open, run must be reported, got %v", got)
	}
	h.noteToolEnd(run, "b")
	if got := h.runsWithOpenTool(); len(got) != 0 {
		t.Fatalf("all tools ended, got %v", got)
	}
}

// Runtimes can drop a tool end (or error out mid-tool). The lifecycle end is
// the backstop: whatever is still open for that run is gone with the turn.
func TestLifecycleEndClearsDanglingTools(t *testing.T) {
	h := newCancelTestHandler()
	h.noteToolStart("device-chat-7-1787885628360", "never-ends")
	h.noteToolStart("device-chat-8-1787885628360", "other-run")

	h.clearOpenTools("device-chat-7-1787885628360")

	got := h.runsWithOpenTool()
	sort.Strings(got)
	if len(got) != 1 || got[0] != "device-chat-8-1787885628360" {
		t.Fatalf("only the cleared run must disappear, got %v", got)
	}
}

// An end for a call we never saw start (event reordering, restart) is a no-op.
func TestUnknownToolEndIsIgnored(t *testing.T) {
	h := newCancelTestHandler()
	h.noteToolEnd("device-chat-9-1787885628360", "ghost")
	if got := h.runsWithOpenTool(); len(got) != 0 {
		t.Fatalf("no open tool expected, got %v", got)
	}
}

// Empty ids are dropped rather than pinning a "" run open forever.
func TestEmptyRunIDIsIgnored(t *testing.T) {
	h := newCancelTestHandler()
	h.noteToolStart("", "call")
	if got := h.runsWithOpenTool(); len(got) != 0 {
		t.Fatalf("empty run id must not be tracked, got %v", got)
	}
}

// The scenario from #419: the main agent is running /servo/search for the
// pen, the user says "hey", the realtime agent replies. That reply must NOT
// take the speaker from the search result still on its way.
func TestRealtimeHandledDoesNotMuteTurnMidTool(t *testing.T) {
	t.Setenv("OS_REALTIME_SUPERSEDES_MAIN_REPLY", "1")
	h := newCancelTestHandler()
	older := deviceRunID(5, time.Now().Add(-2*time.Second))
	h.noteToolStart(older, "servo-search")

	if h.CancelSpeechForNewerTurn() {
		t.Fatalf("mark must not be reported as applied while a tool is open")
	}
	if h.isSpeechCancelled(older) {
		t.Fatalf("turn executing a tool must keep the speaker")
	}
}

// Fillers are part of that promise: the "one moment" for a turn that WILL
// answer must not be dropped either.
func TestRealtimeHandledMidToolKeepsFillers(t *testing.T) {
	t.Setenv("OS_REALTIME_SUPERSEDES_MAIN_REPLY", "1")
	h := newCancelTestHandler()
	fm := sensinghttp.DefaultFillerManager
	runID := "device-chat-56-1787885629999"
	fm.MarkVoiceRun(runID, "")
	fm.OnTurnStart(runID)
	t.Cleanup(func() { fm.Cancel(runID) })
	h.noteToolStart(runID, "web_search")

	h.CancelSpeechForNewerTurn()

	if !fm.HasActiveRun(runID) {
		t.Errorf("filler state of a mid-tool turn must be left alone")
	}
}

// Once the tool ends the ordinary rule is back: a later realtime reply mutes
// the older turn as before.
func TestSupersedeResumesAfterToolEnds(t *testing.T) {
	t.Setenv("OS_REALTIME_SUPERSEDES_MAIN_REPLY", "1")
	h := newCancelTestHandler()
	older := deviceRunID(5, time.Now().Add(-2*time.Second))
	h.noteToolStart(older, "call")
	h.noteToolEnd(older, "call")

	if !h.CancelSpeechForNewerTurn() {
		t.Fatalf("no tool open — mark must be applied")
	}
	if !h.isSpeechCancelled(older) {
		t.Fatalf("older turn must be muted once no tool is open")
	}
}
