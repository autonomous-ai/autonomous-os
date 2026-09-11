package server

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
)

func TestServiceRestart(t *testing.T) {
	for _, tc := range []struct {
		name   string
		target string
		err    error
		status int
	}{
		{name: "HAL", target: "hal", status: http.StatusAccepted},
		{name: "OS server", target: "os-server", status: http.StatusAccepted},
		{name: "unsupported service", target: "ssh", status: http.StatusBadRequest},
		{name: "option injection", target: "--help", status: http.StatusBadRequest},
		{name: "shell injection", target: "hal;reboot", status: http.StatusBadRequest},
		{name: "launch failure", target: "hal", err: errors.New("systemd unavailable"), status: http.StatusInternalServerError},
	} {
		t.Run(tc.name, func(t *testing.T) {
			called := false
			run := func(ctx context.Context, name string, args ...string) ([]byte, error) {
				called = true
				if name != "systemd-run" || !reflect.DeepEqual(args, []string{"--collect", "--on-active=2s", "systemctl", "restart", tc.target}) {
					t.Fatalf("unexpected command: %s %v", name, args)
				}
				deadline, ok := ctx.Deadline()
				if !ok || time.Until(deadline) <= 0 || time.Until(deadline) > 5*time.Second {
					t.Fatalf("expected command timeout within five seconds, got %v", deadline)
				}
				return nil, tc.err
			}
			router := gin.New()
			router.POST("/api/system/restart/:target", serviceRestartHandler(run))
			response := httptest.NewRecorder()
			router.ServeHTTP(response, httptest.NewRequest(http.MethodPost, "/api/system/restart/"+tc.target, nil))
			if response.Code != tc.status {
				t.Fatalf("status = %d, want %d: %s", response.Code, tc.status, response.Body.String())
			}
			if called != (tc.status != http.StatusBadRequest) {
				t.Fatalf("command called = %v for status %d", called, tc.status)
			}
			var body struct {
				Status int `json:"status"`
				Data   *struct {
					Target    string `json:"target"`
					Scheduled bool   `json:"scheduled"`
				} `json:"data"`
				Message *string `json:"message"`
			}
			if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
				t.Fatal(err)
			}
			if tc.status == http.StatusAccepted {
				if body.Status != 1 || body.Data == nil || body.Data.Target != tc.target || !body.Data.Scheduled || body.Message != nil {
					t.Fatalf("unexpected success: %s", response.Body.String())
				}
			} else if body.Status != 0 || body.Data != nil || body.Message == nil {
				t.Fatalf("unexpected error: %s", response.Body.String())
			}
		})
	}
}
