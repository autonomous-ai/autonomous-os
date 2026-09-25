package http

import (
	"errors"
	"testing"
	"time"

	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/monitor"
)

func TestHarnessGroupedResultPublishesOneAnswerAndReferences(t *testing.T) {
	bus := monitor.ProvideBus()
	events, unsub := bus.Subscribe()
	defer unsub()
	h := &AgentHandler{monitorBus: bus}
	h.MarkHarnessResponseRun("a", true, false)
	h.MarkHarnessResponseRun("b", true, false)
	if !h.DeliverHarnessGroupedResult("result-1", "completed", "House with trees", []string{"a", "b"}) {
		t.Fatal("not delivered")
	}
	first, second := <-events, <-events
	if first.Summary != "House with trees" || first.RunID != "a" || second.Summary == first.Summary || second.RunID != "b" {
		t.Fatalf("wrong group output: %+v %+v", first, second)
	}
	if h.DeliverHarnessGroupedResult("result-1", "completed", "House with trees", []string{"a", "b"}) {
		t.Fatal("duplicate delivered")
	}
	if h.DeliverHarnessResponse("b", "late legacy summary") {
		t.Fatal("legacy response escaped tombstone")
	}
}

func TestHarnessGroupedResultRejectsMembershipAtomically(t *testing.T) {
	for _, ids := range [][]string{{"a", "missing"}, {"a", "a"}, {"a", "local"}} {
		h := &AgentHandler{}
		h.MarkHarnessResponseRun("a", true, false)
		h.MarkHarnessLocalResponseRun("local", true)
		if h.DeliverHarnessGroupedResult("r", "completed", "Result", ids) {
			t.Fatal("invalid membership accepted")
		}
		if h.harnessReplies["a"].delivered {
			t.Fatal("partially closed group")
		}
	}
}

func TestHarnessGroupedSpeechSingleSynchronousSubmission(t *testing.T) {
	h := &AgentHandler{}
	h.MarkHarnessResponseRun("a", false, false)
	h.MarkHarnessResponseRun("b", false, false)
	calls := 0
	send := func(text, owner string) error {
		calls++
		if text != "Result" || owner != "a" {
			t.Fatalf("wrong submission %q %q", text, owner)
		}
		return nil
	}
	if err := h.speakHarnessGroupedResult("Result", []string{"a", "b"}, send); err != nil {
		t.Fatal(err)
	}
	if calls != 1 {
		t.Fatalf("calls %d", calls)
	}
}

func TestHarnessGroupedSpeechRejectsAnyIneligibleMember(t *testing.T) {
	for _, reason := range []string{"web", "local", "expired", "missing", "cancelled", "restored"} {
		t.Run(reason, func(t *testing.T) {
			h := &AgentHandler{}
			h.MarkHarnessResponseRun("a", false, false)
			h.MarkHarnessResponseRun("b", false, false)
			state := h.harnessReplies["b"]
			switch reason {
			case "web":
				state.webChat = true
			case "local":
				state.localOnly = true
			case "restored":
				state.restored = true
			case "expired":
				state.created = time.Now().Add(-16 * time.Minute)
			}
			h.harnessReplies["b"] = state
			if reason == "missing" {
				delete(h.harnessReplies, "b")
			}
			if reason == "cancelled" {
				h.speechWatermarkMs.Store(time.Now().UnixMilli() + 1)
			}
			err := h.speakHarnessGroupedResult("Result", []string{"a", "b"}, func(string, string) error { t.Fatal("submitted suppressed result"); return nil })
			if !errors.Is(err, ErrHarnessResultSpeechSuppressed) {
				t.Fatalf("wrong error %v", err)
			}
		})
	}
}

func TestHarnessGroupedSpeechPreservesUnknownAndMutedOutcomes(t *testing.T) {
	for _, sendErr := range []error{errors.New("lost HAL acknowledgement"), hal.ErrSpeakerMuted} {
		h := &AgentHandler{}
		h.MarkHarnessResponseRun("a", false, false)
		calls := 0
		err := h.speakHarnessGroupedResult("Result", []string{"a"}, func(string, string) error { calls++; return sendErr })
		if calls != 1 {
			t.Fatal("retried request")
		}
		if errors.Is(sendErr, hal.ErrSpeakerMuted) {
			if !errors.Is(err, ErrHarnessResultSpeechSuppressed) {
				t.Fatal(err)
			}
		} else if err != sendErr {
			t.Fatal("lost uncertainty", err)
		}
	}
}

func TestHarnessQuestionKeepsResultPendingAndDeduplicatesReplay(t *testing.T) {
	bus := monitor.ProvideBus()
	events, unsub := bus.Subscribe()
	defer unsub()
	h := &AgentHandler{monitorBus: bus}
	h.MarkHarnessResponseRun("a", true, false)
	for _, id := range []string{"q1", "q2"} {
		if !h.DeliverHarnessQuestion("a", id, "Choose a color") {
			t.Fatal("question missing")
		}
		event := <-events
		if event.Type != "assistant_delta" || event.Summary != "Choose a color" {
			t.Fatalf("wrong event %+v", event)
		}
	}
	if h.DeliverHarnessQuestion("a", "q1", "replay") {
		t.Fatal("replayed old question")
	}
	if h.harnessReplies["a"].delivered || h.harnessReplies["a"].localOnly {
		t.Fatal("question consumed remote result")
	}
	if !h.DeliverHarnessGroupedResult("r", "completed", "Final answer", []string{"a"}) {
		t.Fatal("question prevented result")
	}
	if h.DeliverHarnessQuestion("a", "q3", "late") {
		t.Fatal("question reopened completed result")
	}
}

func TestHarnessQuestionRejectsUnavailableRoutes(t *testing.T) {
	h := &AgentHandler{}
	h.MarkHarnessLocalResponseRun("local", true)
	h.MarkHarnessResponseRun("expired", true, false)
	state := h.harnessReplies["expired"]
	state.created = time.Now().Add(-16 * time.Minute)
	h.harnessReplies["expired"] = state
	for _, id := range []string{"", "unknown", "local", "expired"} {
		if h.DeliverHarnessQuestion(id, "q", "Question") {
			t.Fatal("invalid route", id)
		}
	}
}

func TestHarnessGroupedUsageLimitNeverRewritesResult(t *testing.T) {
	h := &AgentHandler{}
	h.MarkHarnessResponseRun("a", false, false)
	err := h.speakHarnessGroupedResult("You've reached the usage limit", []string{"a"}, func(string, string) error { t.Fatal("rewritten/submitted banner"); return nil })
	if !errors.Is(err, ErrHarnessResultSpeechSuppressed) {
		t.Fatal(err)
	}
}
