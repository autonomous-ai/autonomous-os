package http

import (
	"errors"
	"fmt"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/lib/hal"
	sensinghttp "go.autonomous.ai/os/system/server/sensing/delivery/http"
)

// ErrHarnessResultSpeechSuppressed identifies a local policy refusal before submission.
var ErrHarnessResultSpeechSuppressed = errors.New("Harness result speech suppressed")

// DeliverHarnessGroupedResult closes a validated result's member routes together.
// The durable correlation ledger owns result identity and replay deduplication;
// this sink publishes one answer and explicit references on the other runs.
// It never submits speech or fabricates an independent answer for each input.
func (h *AgentHandler) DeliverHarnessGroupedResult(resultID, outcome, text string, runIDs []string) bool {
	if resultID == "" || text == "" || len(runIDs) == 0 {
		return false
	}
	switch outcome {
	case "completed", "failed", "cancelled":
	default:
		return false
	}
	h.harnessRepliesMu.Lock()
	seen := make(map[string]bool, len(runIDs))
	for _, id := range runIDs {
		state, ok := h.harnessReplies[id]
		if id == "" || seen[id] || !ok || state.delivered || state.localOnly {
			h.harnessRepliesMu.Unlock()
			return false
		}
		seen[id] = true
	}
	for _, id := range runIDs {
		state := h.harnessReplies[id]
		state.delivered = true
		h.harnessReplies[id] = state
	}
	h.harnessRepliesMu.Unlock()
	for i, id := range runIDs {
		sensinghttp.DefaultFillerManager.Cancel(id)
		message := text
		if i != 0 {
			message = "See the shared reply for these requests."
		}
		details := map[string]string{"role": "assistant", "message": message, "source": "harness", "result_id": resultID, "outcome": outcome, "result_run_id": runIDs[0]}
		if i != 0 {
			details["result_reference"] = "true"
		}
		flow.Log("harness_response", map[string]any{"run_id": id, "text": message, "result_id": resultID, "result_run_id": runIDs[0], "outcome": outcome, "result_reference": i != 0}, id)
		if h.monitorBus != nil {
			h.monitorBus.Push(domain.MonitorEvent{Type: "chat_response", Summary: message, RunID: id, State: "final", Detail: details})
		}
	}
	return true
}

// SpeakHarnessGroupedResult submits exactly one synchronous HAL request. Nil
// means HAL accepted the request, never proof of playback. The caller must claim
// its durable outbox before calling and must not retry an uncertain submission.
func (h *AgentHandler) SpeakHarnessGroupedResult(text string, runIDs []string) error {
	return h.speakHarnessGroupedResult(text, runIDs, hal.SpeakHarnessReplyForTurn)
}

func (h *AgentHandler) speakHarnessGroupedResult(text string, runIDs []string, send func(string, string) error) error {
	if text == "" || len(runIDs) == 0 {
		return fmt.Errorf("%w: no speech or members", ErrHarnessResultSpeechSuppressed)
	}
	h.harnessRepliesMu.Lock()
	seen := make(map[string]bool, len(runIDs))
	for _, id := range runIDs {
		state, ok := h.harnessReplies[id]
		if id == "" || seen[id] || !ok || state.webChat || state.localOnly || state.restored || time.Since(state.created) > 15*time.Minute {
			h.harnessRepliesMu.Unlock()
			return fmt.Errorf("%w: member %q is not an eligible voice route", ErrHarnessResultSpeechSuppressed, id)
		}
		seen[id] = true
	}
	h.harnessRepliesMu.Unlock()
	for _, id := range runIDs {
		if h.isSpeechCancelled(id) {
			flow.Log("tts_cancelled", map[string]any{"run_id": id, "source": h.speechCancelSource(id)}, id)
			return fmt.Errorf("%w: member %q lost the speaker", ErrHarnessResultSpeechSuppressed, id)
		}
	}
	// Immutable correlated results are never replaced by a local paraphrase.
	if isLLMLimitText(text) {
		return fmt.Errorf("%w: usage-limit banner is display-only", ErrHarnessResultSpeechSuppressed)
	}
	for _, id := range runIDs {
		defer hal.BeginVoiceFollowupSpeech(id)()
	}
	err := send(text, runIDs[0])
	if errors.Is(err, hal.ErrSpeakerMuted) {
		flow.Log("tts_muted", map[string]any{"run_id": runIDs[0], "text": text}, runIDs[0])
		return fmt.Errorf("%w: %v", ErrHarnessResultSpeechSuppressed, err)
	}
	return err
}

// DeliverHarnessQuestion exposes a structured question without consuming the result
// route. Deduplication is per original run and question ID, including replay
// after another question; final result ownership stays with the result ledger.
func (h *AgentHandler) DeliverHarnessQuestion(runID, questionID, text string) bool {
	if runID == "" || questionID == "" || text == "" {
		return false
	}
	h.harnessRepliesMu.Lock()
	state, ok := h.harnessReplies[runID]
	if !ok || state.delivered || state.localOnly || time.Since(state.created) > 15*time.Minute || state.questionIDs[questionID] || len(state.questionIDs) >= 64 {
		h.harnessRepliesMu.Unlock()
		return false
	}
	if state.questionIDs == nil {
		state.questionIDs = map[string]bool{}
	}
	state.questionIDs[questionID] = true
	h.harnessReplies[runID] = state
	h.harnessRepliesMu.Unlock()
	sensinghttp.DefaultFillerManager.Cancel(runID)
	if h.monitorBus != nil {
		h.monitorBus.Push(domain.MonitorEvent{Type: "assistant_delta", Summary: text, RunID: runID, Detail: map[string]string{"role": "assistant", "source": "harness", "question_id": questionID}})
	}
	if !state.webChat && !state.restored {
		h.deliverTTS(hal.SpeakHarnessReply, text, runID, "speak Harness question")
	}
	return true
}
