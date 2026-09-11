package http

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
)

type queuedVoiceGateway struct {
	busyGateway
	fixedRun string
}

func (g *queuedVoiceGateway) NextChatRunID() (string, string) {
	return "request-voice", "queued-voice-run"
}
func (g *queuedVoiceGateway) QueuePendingEvent(_, _ string, _ []string, run string) { g.fixedRun = run }

func TestVoiceTaskMetricSurvivesMissingHALAndQueuedReplay(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, queued := range []bool{false, true} {
		t.Run(map[bool]string{false: "not_ready", true: "queued"}[queued], func(t *testing.T) {
			var logs bytes.Buffer
			previous := slog.Default()
			slog.SetDefault(slog.New(slog.NewJSONHandler(&logs, nil)))
			defer slog.SetDefault(previous)
			h := &SensingHandler{monitorBus: monitor.ProvideBus(), config: &config.Config{}}
			gw := &queuedVoiceGateway{}
			if queued {
				h.agentGateway = gw
			} else {
				h.agentGateway = &idleGateway{}
			}
			rec := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(rec)
			c.Request = httptest.NewRequest(http.MethodPost, "/api/sensing/event", strings.NewReader(`{"type":"voice","message":"calculate nineteen times twenty three"}`))
			c.Request.Header.Set("Content-Type", "application/json")
			h.PostEvent(c)
			var starts []map[string]any
			var outcomes []map[string]any
			for _, line := range bytes.Split(logs.Bytes(), []byte("\n")) {
				var row map[string]any
				if json.Unmarshal(line, &row) != nil || row["msg"] != "[telemetry] event" {
					continue
				}
				var params map[string]any
				if err := json.Unmarshal([]byte(row["params"].(string)), &params); err != nil {
					t.Fatal(err)
				}
				if row["event_name"] == "voice_metrics_task_started" {
					starts = append(starts, params)
				}
				if row["event_name"] == "voice_metrics_task_execution" {
					outcomes = append(outcomes, params)
				}
			}
			if len(starts) != 2 {
				t.Fatalf("expected receipt and binding, got %d; response %s", len(starts), rec.Body.String())
			}
			id := starts[0]["interaction_id"].(string)
			if !strings.HasPrefix(id, "os-voice-") || starts[1]["interaction_id"] != id {
				t.Fatalf("lost identity: %+v", starts)
			}
			if queued {
				if gw.fixedRun != "queued-voice-run" || starts[1]["run_id"] != gw.fixedRun {
					t.Fatalf("queue lost metric correlation: %+v", starts)
				}
				if len(outcomes) != 0 {
					t.Fatal("queued turn must remain incomplete")
				}
			} else {
				if rec.Code != http.StatusServiceUnavailable || len(outcomes) != 1 || outcomes[0]["outcome"] != "failed" || outcomes[0]["interaction_id"] != id {
					t.Fatalf("not-ready task missing failure: %+v", outcomes)
				}
			}
		})
	}
}

func (g *queuedVoiceGateway) MarkWebChatRun(string) {}

func TestChatAndSensingTaskCohortsAtRoutingBoundaries(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, eventType := range []string{"web_chat", "mqtt_chat", "motion.activity"} {
		for _, queued := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/queued=%t", eventType, queued), func(t *testing.T) {
				var logs bytes.Buffer
				previous := slog.Default()
				slog.SetDefault(slog.New(slog.NewJSONHandler(&logs, nil)))
				defer slog.SetDefault(previous)
				h := &SensingHandler{monitorBus: monitor.ProvideBus(), config: &config.Config{}}
				gw := &queuedVoiceGateway{}
				if queued {
					h.agentGateway = gw
				} else {
					h.agentGateway = &idleGateway{}
				}
				rec := httptest.NewRecorder()
				c, _ := gin.CreateTestContext(rec)
				body := fmt.Sprintf(`{"type":%q,"message":"test source task"}`, eventType)
				c.Request = httptest.NewRequest(http.MethodPost, "/api/sensing/event", strings.NewReader(body))
				c.Request.Header.Set("Content-Type", "application/json")
				h.PostEvent(c)
				var starts, failures int
				group := "chat"
				if eventType == "motion.activity" {
					group = "sensing"
				}
				for _, line := range bytes.Split(logs.Bytes(), []byte("\n")) {
					var row map[string]any
					if json.Unmarshal(line, &row) != nil || row["msg"] != "[telemetry] event" {
						continue
					}
					if row["event_name"] == "voice_metrics_task_started" {
						t.Fatal("non-voice source contaminated voice cohort")
					}
					if row["event_name"] == group+"_metrics_task_started" {
						starts++
					}
					if row["event_name"] == "voice_metrics_task_execution" {
						failures++
					}
				}
				wantStarts, wantFailures := 2, 1
				if queued {
					wantFailures = 0
				}
				if group == "sensing" {
					wantStarts = 1
					if queued {
						wantStarts = 0
					}
				}
				if starts != wantStarts || failures != wantFailures {
					t.Fatalf("starts=%d failures=%d; want %d/%d; response=%s", starts, failures, wantStarts, wantFailures, rec.Body.String())
				}
				if queued && group == "chat" && gw.fixedRun == "" {
					t.Fatal("chat queue must retain run binding")
				}
			})
		}
	}
}
