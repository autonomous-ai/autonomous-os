package picoclaw

import (
	"bytes"
	"encoding/json"
	"log/slog"
	"strings"
	"testing"
	"time"
)

func TestSensingReplayMetricStartsAfterFiltersAndReusesUnsentRun(t *testing.T) {
	s := queueService(t)
	var logs bytes.Buffer
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(&logs, nil)))
	t.Cleanup(func() { slog.SetDefault(previous) })
	s.wsConnected.Store(true) // Readiness raced with losing the actual socket.
	s.pendingEvents = []pendingEvent{
		{eventType: "motion.activity", msg: "expired", queuedAt: time.Now().Add(-2 * time.Minute)},
		{eventType: "presence.enter", msg: "superseded", queuedAt: time.Now()},
		{eventType: "presence.enter", msg: "retained", queuedAt: time.Now()},
	}
	s.drainPendingEvents()
	if len(s.pendingEvents) != 1 || s.pendingEvents[0].fixedRunID == "" {
		t.Fatalf("unsent retained cohort lost correlation: %+v", s.pendingEvents)
	}
	runID := s.pendingEvents[0].fixedRunID
	frames := queueSocket(t, s)
	s.drainPendingEvents()
	nextQueuedFrame(t, frames, runID)

	starts, failures := 0, 0
	interactionID := ""
	for _, line := range strings.Split(strings.TrimSpace(logs.String()), "\n") {
		var entry struct {
			Name   string `json:"event_name"`
			Params string `json:"params"`
		}
		if err := json.Unmarshal([]byte(line), &entry); err != nil {
			t.Fatal(err)
		}
		if entry.Name == "voice_metrics_task_execution" || entry.Name == "task_metrics_execution" {
			failures++
		}
		if entry.Name != "sensing_metrics_task_started" {
			continue
		}
		starts++
		var params map[string]any
		if err := json.Unmarshal([]byte(entry.Params), &params); err != nil {
			t.Fatal(err)
		}
		if params["run_id"] != runID || params["event_type"] != "presence.enter" {
			t.Fatalf("unexpected sensing cohort: %v", params)
		}
		got, _ := params["interaction_id"].(string)
		if got == "" || (interactionID != "" && got != interactionID) {
			t.Fatalf("retry changed cohort identity: %v", params)
		}
		interactionID = got
	}
	if starts != 2 || failures != 0 {
		t.Fatalf("want two observations of one cohort and no terminal failures, got starts=%d failures=%d", starts, failures)
	}
}

func TestSensingReplayWriteFailureReportsTerminalForStartedRun(t *testing.T) {
	s := queueService(t)
	queueSocket(t, s)
	_ = s.wsConn.Close()
	var logs bytes.Buffer
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(&logs, nil)))
	t.Cleanup(func() { slog.SetDefault(previous) })
	s.pendingEvents = []pendingEvent{{eventType: "presence.enter", msg: "retained", queuedAt: time.Now()}}
	s.drainPendingEvents()
	var startedRun string
	failures := 0
	for _, line := range strings.Split(strings.TrimSpace(logs.String()), "\n") {
		var entry struct {
			Name   string `json:"event_name"`
			Params string `json:"params"`
		}
		if err := json.Unmarshal([]byte(line), &entry); err != nil {
			t.Fatal(err)
		}
		if entry.Name != "sensing_metrics_task_started" && entry.Name != "voice_metrics_task_execution" {
			continue
		}
		var params map[string]any
		if err := json.Unmarshal([]byte(entry.Params), &params); err != nil {
			t.Fatal(err)
		}
		if entry.Name == "sensing_metrics_task_started" {
			startedRun, _ = params["run_id"].(string)
			continue
		}
		failures++
		if startedRun == "" || params["run_id"] != startedRun || params["outcome"] != "failed" || params["evidence"] != "dispatch_error" {
			t.Fatalf("wrong terminal correlation/verdict: %v", params)
		}
	}
	if failures != 1 || len(s.pendingEvents) != 0 {
		t.Fatalf("want one failed execution with no retry, failures=%d queue=%+v", failures, s.pendingEvents)
	}
}
