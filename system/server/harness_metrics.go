package server

import (
	"errors"
	"strings"
	"time"

	"go.autonomous.ai/os/system/harness"
	"go.autonomous.ai/os/system/telemetry"
)

func reportHarnessDispatchError(runID, interactionID string, err error) {
	if err == nil {
		return
	}
	var uncertain *harness.DeliveryUnknownError
	if errors.As(err, &uncertain) {
		telemetry.ReportTaskExecution(runID, interactionID, "unknown", "execution_observation_lost")
		return
	}
	telemetry.ReportTaskExecution(runID, interactionID, "failed", "dispatch_error")
}

// observeHarnessExecution is independent of text delivery: done needs no recap,
// errors need no display text, and asking a question is not execution success.
func (s *Server) observeHarnessExecution(agentID, kind string, frame harness.Frame) {
	var outcome, evidence string
	switch kind {
	case "turn.done":
		outcome, evidence = "completed", "harness_turn_done"
	case "turn.summary":
		if strings.TrimSpace(harnessEventText(kind, frame)) == "" {
			return
		}
		outcome, evidence = "completed", "harness_turn_summary"
	case "turn.error", "agent.error":
		outcome, evidence = "failed", "harness_turn_error"
	case "question.open":
		outcome, evidence = "unknown", "harness_question_open"
	default:
		return
	}
	// Preserve legacy reply routing. Metrics must not guess the oldest turn
	// when an explicit ID mismatches or several requests share an agent.
	eventRunID := harnessFrameRunID(frame)
	s.harnessRepliesMu.Lock()
	var runID string
	for _, reply := range s.harnessReplies {
		if reply.agentID != agentID || time.Since(reply.created) > 15*time.Minute {
			continue
		}
		if eventRunID != "" && reply.runID != eventRunID {
			continue
		}
		if runID != "" {
			runID = ""
			break
		}
		runID = reply.runID
	}
	s.harnessRepliesMu.Unlock()
	if runID != "" {
		telemetry.ReportTaskExecution(runID, "", outcome, evidence)
	}
}

// VoiceController uses this callback when an answer is stored locally and it
// must ask another structured question before dispatching anything remotely.
func (s *Server) deliverHarnessVoiceQuestion(agentID, runID, text string) {
	telemetry.ReportTaskExecution(runID, "", "unknown", "harness_question_open")
	s.deliverHarnessVoiceMessage(agentID, runID, text)
}

// A rejection is definitive dispatch failure. Other receipt states only
// acknowledge the command; execution completion still needs lifecycle evidence.
func reportHarnessReceipt(runID string, frame harness.Frame) {
	receipt, _ := frame["receipt"].(map[string]any)
	if state, _ := receipt["state"].(string); state == "rejected" {
		telemetry.ReportTaskExecution(runID, "", "failed", "dispatch_error")
	}
}
