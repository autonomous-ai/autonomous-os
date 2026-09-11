package telemetry

import (
	"encoding/json"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
)

func TestTaskLifecycleEndRejectsAbortedAndErrorFrames(t *testing.T) {
	cases := []struct {
		name, data, outcome, evidence string
	}{
		{"completed", `{"phase":"end","aborted":false,"stopReason":"stop"}`, "completed", "lifecycle_end"},
		{"aborted", `{"phase":"end","aborted":true}`, "failed", "lifecycle_end_error"},
		{"error", `{"phase":"end","error":"private runtime error"}`, "failed", "lifecycle_end_error"},
		{"both", `{"phase":"end","aborted":true,"error":"runtime error"}`, "failed", "lifecycle_end_error"},
		{"legacy", `{"phase":"end"}`, "completed", "lifecycle_end"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			m := newMock(nil, 1)
			withPipe(t, m)
			var payload domain.AgentPayload
			if err := json.Unmarshal([]byte(`{"runId":"device-terminal","stream":"lifecycle","data":`+tc.data+`}`), &payload); err != nil {
				t.Fatal(err)
			}
			ReportTaskLifecycleEnd(payload.RunID, payload.Data.Aborted, payload.Data.Error != "")
			m.wait(t, 1)
			params := m.events[0].params
			if params["outcome"] != tc.outcome || params["evidence"] != tc.evidence || params["error"] != (tc.outcome == "failed") {
				t.Fatalf("incorrect terminal verdict: %+v", params)
			}
		})
	}
}

func TestTaskExecutionPreservesEvidenceAndCorrelation(t *testing.T) {
	m := newMock(nil, 6)
	withPipe(t, m)
	start := time.Now().UnixMilli()
	cases := []struct{ run, interaction, outcome, evidence string }{
		{"device-1", "", "completed", "lifecycle_end"},
		{"device-2", "", "failed", "lifecycle_error"},
		{"device-3", "", "unknown", "lifecycle_error_recovered"},
		{"device-4", "", "failed", "chat_error"},
		{"", "vi-local", "completed", "local_intent_returned"},
		{"", "vi-local-failed", "failed", "local_intent_error"},
	}
	for _, tc := range cases {
		ReportTaskExecution(tc.run, tc.interaction, tc.outcome, tc.evidence)
	}
	m.wait(t, len(cases))
	seen := map[string]bool{}
	for i, tc := range cases {
		got := m.events[i]
		if got.name != "voice_metrics_task_execution" {
			t.Fatalf("event name = %q", got.name)
		}
		for key, want := range map[string]any{
			"run_id": tc.run, "interaction_id": tc.interaction,
			"outcome": tc.outcome, "evidence": tc.evidence,
			"schema_version": 1, "error": tc.outcome != "completed",
		} {
			if got.params[key] != want {
				t.Errorf("%s[%s] = %v, want %v", tc.evidence, key, got.params[key], want)
			}
		}
		stamp, ok := got.params["execution_at_ms"].(int64)
		if !ok || stamp < start || stamp > time.Now().UnixMilli() {
			t.Errorf("invalid execution timestamp: %v", got.params["execution_at_ms"])
		}
		id, _ := got.params["event_id"].(string)
		if id == "" || seen[id] {
			t.Errorf("missing or duplicate event ID %q", id)
		}
		seen[id] = true
	}
}

func TestTaskExecutionRejectsUncorrelatedAndUnboundedValues(t *testing.T) {
	m := newMock(nil, 1)
	withPipe(t, m)
	ReportTaskExecution("", "", "completed", "lifecycle_end")
	ReportTaskExecution("device-1", "", "sensitive error text", "lifecycle_error")
	ReportTaskExecution("device-1", "", "failed", "sensitive tool result")
	ReportTaskExecution("device-1", "", "completed", "lifecycle_error_recovered")
	ReportTaskExecution("device-1", "", "completed", "local_intent_error")
	ReportTaskExecution("device-1", "", "completed", "lifecycle_end_error")
	ReportTaskExecution("device-1", "", "completed", "lifecycle_end")
	m.wait(t, 1)
	if len(m.events) != 1 || m.events[0].params["evidence"] != "lifecycle_end" {
		t.Fatalf("unexpected observations: %+v", m.events)
	}
}
