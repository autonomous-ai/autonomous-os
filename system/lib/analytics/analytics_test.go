package analytics

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestTrackEvent(t *testing.T) {
	var gotAuth string
	var gotBody map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		_ = json.NewDecoder(r.Body).Decode(&gotBody)
	}))
	defer srv.Close()

	t.Setenv(envKey, "test-key")
	t.Setenv("AUTONOMOUS_ANALYTICS_URL", srv.URL)

	if err := TrackEvent(context.Background(), "device_booted", map[string]any{
		"device_type": "lamp",
		"skipped":     nil,
	}); err != nil {
		t.Fatalf("TrackEvent: %v", err)
	}

	if gotAuth != "test-key" {
		t.Errorf("Authorization = %q, want test-key", gotAuth)
	}
	if gotBody["event_name"] != "device_booted" {
		t.Errorf("event_name = %v", gotBody["event_name"])
	}
	data := gotBody["data"].(map[string]any)
	if data["platform"] != platform {
		t.Errorf("platform = %v", data["platform"])
	}
	params := data["event_params"].([]any)
	if len(params) != 1 {
		t.Fatalf("event_params = %v, want nil value dropped", params)
	}
	p := params[0].(map[string]any)
	if p["key"] != "device_type" || p["value"] != "lamp" {
		t.Errorf("event_params[0] = %v", p)
	}
}
