package http

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
)

// busyGateway embeds domain.AgentGateway so only the methods this path calls
// are real; any other call panics on the nil embedded interface, which is fine
// because these tests never reach them. Mirrors the fakeGateway pattern used
// elsewhere in this repo.
type busyGateway struct {
	domain.AgentGateway
	queued int
}

func (g *busyGateway) IsBusy() bool { return true }
func (g *busyGateway) Name() string { return "fake" }
func (g *busyGateway) QueuePendingEvent(eventType, msg string, images []string, fixedRunID string) {
	g.queued++
}

func postRealtimeHandled(t *testing.T, h *SensingHandler) *httptest.ResponseRecorder {
	t.Helper()
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	body := `{"type":"voice_agent_handled","message":"[HANDLED] what time is it\n[REPLY] just past two"}`
	c.Request = httptest.NewRequest(http.MethodPost, "/api/sensing/event", bytes.NewBufferString(body))
	c.Request.Header.Set("Content-Type", "application/json")
	h.PostEvent(c)
	return rec
}

// The regression this whole hook exists for. voice_agent_handled counts as
// passive, so a BUSY agent queues it and returns early — and "the agent is
// busy" is exactly the case with an older turn still in flight. Hooking the
// mark further down, next to MarkSilentRun, made it a no-op precisely when it
// was needed.
func TestRealtimeHandledHookFiresEvenWhenTheAgentIsBusy(t *testing.T) {
	gw := &busyGateway{}
	h := &SensingHandler{agentGateway: gw, monitorBus: monitor.ProvideBus(), config: &config.Config{}}
	fired := 0
	h.SetOnRealtimeHandled(func() bool { fired++; return true })

	rec := postRealtimeHandled(t, h)

	if fired != 1 {
		t.Fatalf("hook must fire before the busy fork returns; fired=%d (status %d)", fired, rec.Code)
	}
	if gw.queued != 1 {
		t.Errorf("the sync event itself must still be queued for replay, queued=%d", gw.queued)
	}
}

// The hook is optional wiring — a handler without it must behave as before.
func TestPostEventWithoutHookIsUnaffected(t *testing.T) {
	gw := &busyGateway{}
	h := &SensingHandler{agentGateway: gw, monitorBus: monitor.ProvideBus(), config: &config.Config{}}

	rec := postRealtimeHandled(t, h)

	if rec.Code != http.StatusOK {
		t.Errorf("expected 200 with no hook installed, got %d", rec.Code)
	}
}

// The response must tell HAL whether the older turn ACTUALLY lost the speaker.
// HAL records a stale-reply metrics situation only for real suppressions: with the
// policy off nothing was suppressed, and counting it would inflate the
// denominator with situations that never existed.
func TestRealtimeHandledResponseReportsWhetherSpeechWasSuppressed(t *testing.T) {
	for _, tc := range []struct {
		name      string
		applied   bool
		wantValue bool
	}{
		{"policy on", true, true},
		{"policy off", false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			gw := &busyGateway{}
			h := &SensingHandler{agentGateway: gw, monitorBus: monitor.ProvideBus(), config: &config.Config{}}
			h.SetOnRealtimeHandled(func() bool { return tc.applied })

			rec := postRealtimeHandled(t, h)

			var body struct {
				Data map[string]any `json:"data"`
			}
			if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
				t.Fatalf("decode response: %v (%s)", err, rec.Body.String())
			}
			got, ok := body.Data["speechSuppressed"]
			if !ok {
				t.Fatalf("response must carry speechSuppressed: %s", rec.Body.String())
			}
			if got != tc.wantValue {
				t.Errorf("speechSuppressed = %v, want %v", got, tc.wantValue)
			}
		})
	}
}
