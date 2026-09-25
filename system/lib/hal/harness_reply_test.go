package hal

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
)

func TestHarnessReplyRequestsCueWithoutChangingVoiceOrText(t *testing.T) {
	original := httpClient
	t.Cleanup(func() { httpClient = original })
	for _, harnessResult := range []bool{true, false} {
		httpClient = &http.Client{Transport: environmentTransport(func(r *http.Request) (*http.Response, error) {
			var payload map[string]any
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				t.Fatal(err)
			}
			if r.URL.Path != "/voice/speak" || payload["text"] != "Original answer" || payload["realtime_feedback"] != true {
				t.Fatalf("changed result payload: %v", payload)
			}
			if (payload["harness_result"] == true) != harnessResult || payload["voice"] != nil || payload["provider"] != nil {
				t.Fatalf("wrong cue or voice override: %v", payload)
			}
			return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"status":"ok"}`))}, nil
		})}
		speak := SpeakReply
		if harnessResult {
			speak = SpeakHarnessReply
		}
		if err := speak("Original answer"); err != nil {
			t.Fatal(err)
		}
	}
}
