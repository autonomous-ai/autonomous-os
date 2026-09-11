package mqtthandler

import (
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

type environmentRoundTripper func(*http.Request) (*http.Response, error)

func (f environmentRoundTripper) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestEnvironmentStatusMQTT(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("DEVICES_DIR", dir)
	if err := os.MkdirAll(filepath.Join(dir, "test-body"), 0755); err != nil {
		t.Fatal(err)
	}
	original := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = original })
	for _, tc := range []struct {
		name         string
		declared     bool
		code         int
		body, status string
	}{
		{"undeclared", false, 200, `{}`, "failure"},
		{"generic sensor", true, 200, `{"state":"ready","stale":false,"sample":{"co2_ppm":410}}`, "success"},
		{"disabled", true, 200, `{"state":"disabled","stale":true,"sample":null}`, "success"},
		{"stale", true, 200, `{"state":"ready","stale":true,"sample":{"temperature_c":22}}`, "success"},
		{"sensor error", true, 200, `{"state":"error","stale":true,"last_error":"disconnected"}`, "success"},
		{"hal missing", true, 404, `{}`, "failure"},
		{"invalid", true, 200, `{}`, "failure"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			capLine := "  # environment: {required: false}\n"
			if tc.declared {
				capLine = "  environment: {required: false}\n"
			}
			if err := os.WriteFile(filepath.Join(dir, "test-body", "ROBOT.md"), []byte("---\ncapabilities:\n"+capLine+"---\n"), 0644); err != nil {
				t.Fatal(err)
			}
			calls := 0
			http.DefaultTransport = environmentRoundTripper(func(r *http.Request) (*http.Response, error) {
				calls++
				if r.URL.Path != "/environment/status" {
					t.Fatalf("unexpected path %s", r.URL.Path)
				}
				return &http.Response{StatusCode: tc.code, Body: io.NopCloser(strings.NewReader(tc.body))}, nil
			})
			factory, messages := statusBroker(t)
			h := &DeviceMQTTHandler{config: &config.Config{DeviceType: "test-body", DeviceID: "test", FDChannel: "test/fd"}, mqttFactory: factory}
			if err := h.dispatchData(domain.MQTTDataCommand{Kind: domain.KindEnvironmentStatus}); err != nil {
				t.Fatal(err)
			}
			select {
			case payload := <-messages:
				var reply domain.MQTTDataResponse
				if err := json.Unmarshal(payload, &reply); err != nil {
					t.Fatal(err)
				}
				if reply.Kind != domain.KindEnvironmentStatus || reply.Status != tc.status {
					t.Fatalf("unexpected reply %s", payload)
				}
				if tc.status == "failure" && reply.Error == "" {
					t.Fatalf("missing error: %s", payload)
				}
				if tc.status == "success" {
					var raw map[string]json.RawMessage
					_ = json.Unmarshal(payload, &raw)
					if string(raw["data"]) != tc.body {
						t.Fatalf("snapshot changed: %s", payload)
					}
				}
			case <-time.After(3 * time.Second):
				t.Fatal("missing MQTT reply")
			}
			if !tc.declared && calls != 0 {
				t.Fatal("undeclared hardware was queried")
			}
		})
	}
}
