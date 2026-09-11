package http

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
)

func TestEnvironmentDispatchGates(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, tc := range []struct {
		name                                       string
		declared, sleeping, floor, guard, disabled bool
		want                                       string
		code                                       int
	}{
		{name: "missing capability", want: "environment capability not declared", code: 403},
		{name: "policy disabled", declared: true, disabled: true, want: "dropped_disabled", code: 200},
		{name: "sleeping", declared: true, sleeping: true, want: "dropped_sleeping", code: 200},
		{name: "floor", declared: true, floor: true, want: "dropped_floor", code: 200},
		{name: "guard retains floor", declared: true, floor: true, guard: true, want: "dropped_floor", code: 200},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			t.Setenv("DEVICES_DIR", dir)
			if tc.declared {
				if err := os.MkdirAll(filepath.Join(dir, "test-body"), 0755); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(filepath.Join(dir, "test-body", "ROBOT.md"), []byte("---\ncapabilities:\n  environment: {}\n---\n"), 0644); err != nil {
					t.Fatal(err)
				}
			}
			gw := &idleGateway{}
			h := &SensingHandler{agentGateway: gw, monitorBus: monitor.ProvideBus(), config: &config.Config{DeviceType: "test-body", GuardMode: &tc.guard}, isSleeping: func() bool { return tc.sleeping }}
			if tc.disabled {
				settings := config.DefaultEnvironmentConfig()
				settings.Enabled = false
				h.config.Environment = &settings
			}
			if tc.floor {
				h.lastAgentTurn.Store(time.Now().UnixMilli())
			}
			rec := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(rec)
			c.Request = httptest.NewRequest(http.MethodPost, "/api/sensing/event", bytes.NewBufferString(`{"type":"environment.update","message":"PM2.5 changed"}`))
			c.Request.Header.Set("Content-Type", "application/json")
			h.PostEvent(c)
			if rec.Code != tc.code || !strings.Contains(rec.Body.String(), tc.want) {
				t.Fatalf("response %d %s", rec.Code, rec.Body.String())
			}
			if gw.queued.Load() != 0 {
				t.Fatal("suppressed event must not enter queue")
			}
		})
	}
}

func TestEnvironmentQueuesWhileBusy(t *testing.T) {
	if !shouldQueueEvent("environment.update", "change", false) {
		t.Fatal("environment change must queue while busy")
	}
}
