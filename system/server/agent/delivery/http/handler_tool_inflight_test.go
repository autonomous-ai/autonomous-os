package http

import (
	"sort"
	"testing"
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
