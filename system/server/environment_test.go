package server

import (
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/server/config"
)

type environmentTransport func(*http.Request) (*http.Response, error)

func (f environmentTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestEnvironmentStatusAccessAndEnvelope(t *testing.T) {
	gin.SetMode(gin.TestMode)
	dir := t.TempDir()
	t.Setenv("DEVICES_DIR", dir)
	if err := os.MkdirAll(filepath.Join(dir, "env-test"), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "env-test", "ROBOT.md"), []byte("---\ncapabilities:\n  environment: {}\n---\n"), 0644); err != nil {
		t.Fatal(err)
	}
	original := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = original })
	calls := 0
	http.DefaultTransport = environmentTransport(func(r *http.Request) (*http.Response, error) {
		calls++
		if r.URL.Path != "/environment/status" {
			t.Fatalf("unexpected HAL route %s", r.URL)
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(`{"state":"ready","enabled":true,"stale":false,"age_s":1,"sample":{"temperature_c":27,"voc_index":null}}`)), Header: make(http.Header)}, nil
	})
	for _, tc := range []struct {
		name, device, remote, forwarded string
		code, reads                     int
	}{
		{"loopback", "env-test", "127.0.0.1:12345", "", 200, 1},
		{"missing capability", "missing-test", "127.0.0.1:12345", "", 403, 0},
		{"remote", "env-test", "192.168.1.9:12345", "", 403, 0},
		{"forwarded remote", "env-test", "127.0.0.1:12345", "192.168.1.9", 403, 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			calls = 0
			s := &Server{config: &config.Config{DeviceType: tc.device}}
			r := gin.New()
			r.GET("/api/environment/status", localOnlyMiddleware(), s.environmentStatus)
			req := httptest.NewRequest("GET", "/api/environment/status", nil)
			req.RemoteAddr = tc.remote
			req.Header.Set("X-Forwarded-For", tc.forwarded)
			out := httptest.NewRecorder()
			r.ServeHTTP(out, req)
			if out.Code != tc.code || calls != tc.reads {
				t.Fatalf("code %d reads %d: %s", out.Code, calls, out.Body)
			}
			if tc.code == 200 && (!strings.Contains(out.Body.String(), `"status":1`) || !strings.Contains(out.Body.String(), `"voc_index":null`)) {
				t.Fatalf("lost envelope or null: %s", out.Body)
			}
		})
	}
}
