package hal

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
)

func TestHarnessGroupedReplyPreservesOwnerAndCue(t *testing.T) {
	original := httpClient
	t.Cleanup(func() { httpClient = original })
	httpClient = &http.Client{Transport: environmentTransport(func(r *http.Request) (*http.Response, error) {
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		if r.URL.Path != "/voice/speak" || payload["text"] != "Result" || payload["turn_id"] != "run-a" || payload["realtime_feedback"] != true || payload["harness_result"] != true {
			t.Fatalf("wrong payload: %v", payload)
		}
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"status":"ok"}`))}, nil
	})}
	if err := SpeakHarnessReplyForTurn("Result", "run-a"); err != nil {
		t.Fatal(err)
	}
}
