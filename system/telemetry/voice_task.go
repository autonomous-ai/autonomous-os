package telemetry

import (
	"crypto/rand"
	"time"
)

// ReportTaskLifecycleEnd distinguishes normal completion from aborted/error
// terminal frames. A lifecycle "end" alone does not guarantee execution success.
func ReportTaskLifecycleEnd(runID string, aborted, hasError bool) {
	if aborted || hasError {
		ReportTaskExecution(runID, "", "failed", "lifecycle_end_error")
		return
	}
	ReportTaskExecution(runID, "", "completed", "lifecycle_end")
}

// ReportTaskExecution records an execution boundary, not answer correctness.
// These observations may include non-voice runs; consumers must join them to
// an eligible HAL voice task by run_id or interaction_id before scoring it.
func ReportTaskExecution(runID, interactionID, outcome, evidence string) {
	if runID == "" && interactionID == "" {
		return
	}
	// Keep this payload content-free even if a future caller supplies an
	// unexpected value: errors, transcripts and tool results must not escape.
	switch evidence {
	case "lifecycle_end", "local_intent_returned":
		if outcome != "completed" {
			return
		}
	case "lifecycle_error", "lifecycle_end_error", "chat_error", "local_intent_error":
		if outcome != "failed" {
			return
		}
	case "lifecycle_error_recovered":
		if outcome != "unknown" {
			return
		}
	default:
		return
	}
	Report(Event{
		Name: "voice_metrics_task_execution",
		ID:   "vte-" + rand.Text(),
		Params: map[string]any{
			"schema_version":  1,
			"run_id":          runID,
			"interaction_id":  interactionID,
			"outcome":         outcome,
			"evidence":        evidence,
			"error":           outcome != "completed",
			"execution_at_ms": time.Now().UnixMilli(),
		},
	})
}
