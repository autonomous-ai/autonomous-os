package picoclaw

import (
	"bytes"
	"encoding/json"
	"log/slog"
	"testing"
)

type observationLogWriter chan []byte

func (w observationLogWriter) Write(p []byte) (int, error) {
	if bytes.Contains(p, []byte("execution_observation_lost")) {
		w <- append([]byte(nil), p...)
	}
	return len(p), nil
}

func captureLostObservations(t *testing.T) observationLogWriter {
	t.Helper()
	// Never use a configured device analytics service from this test.
	t.Setenv("AUTONOMOUS_ANALYTICS_URL", "http://127.0.0.1:1/test")
	records := make(observationLogWriter, 8)
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(records, nil)))
	t.Cleanup(func() { slog.SetDefault(previous) })
	return records
}

func assertLostObservation(t *testing.T, records observationLogWriter, runID string) {
	t.Helper()
	if len(records) != 1 {
		t.Fatalf("lost observations = %d, want 1", len(records))
	}
	var record struct {
		Params string `json:"params"`
	}
	if err := json.Unmarshal(<-records, &record); err != nil {
		t.Fatal(err)
	}
	var params map[string]any
	if err := json.Unmarshal([]byte(record.Params), &params); err != nil {
		t.Fatal(err)
	}
	if params["run_id"] != runID || params["outcome"] != "unknown" || params["evidence"] != "execution_observation_lost" {
		t.Fatalf("wrong observation: %v", params)
	}
}
