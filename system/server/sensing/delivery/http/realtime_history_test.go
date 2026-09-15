package http

import (
	"errors"
	"net/http"
	"strings"
	"testing"

	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
)

func TestRealtimeHistoryPersistsBeforeBusyGate(t *testing.T) {
	gw := &busyGateway{}
	h := &SensingHandler{agentGateway: gw, monitorBus: monitor.ProvideBus(), config: &config.Config{}}
	superseded := false
	h.SetOnRealtimeHandled(func() bool { superseded = true; return true })
	h.SetRealtimeHistory(func(origin, message string) (string, error) {
		if !superseded {
			t.Fatal("lost speech supersession before persistence")
		}
		if !strings.Contains(message, "[REPLY] just past two") {
			t.Fatal("lost reply")
		}
		return "device-chat-context-test", nil
	})
	rec := postRealtimeHandled(t, h)
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"speechSuppressed":true`) || !strings.Contains(rec.Body.String(), `"runId":"device-realtime-test"`) || !strings.Contains(rec.Body.String(), `"historyRunId":"device-chat-context-test"`) {
		t.Fatalf("unexpected response: %d %s", rec.Code, rec.Body.String())
	}
	if gw.queued != 0 {
		t.Fatal("also queued volatile duplicate")
	}
}

func TestRealtimeHistoryPersistenceErrorDoesNotFallThrough(t *testing.T) {
	gw := &busyGateway{}
	h := &SensingHandler{agentGateway: gw, monitorBus: monitor.ProvideBus(), config: &config.Config{}}
	h.SetRealtimeHistory(func(string, string) (string, error) { return "", errors.New("disk full") })
	rec := postRealtimeHandled(t, h)
	if rec.Code != http.StatusInternalServerError || gw.queued != 0 {
		t.Fatalf("failed persistence accepted: %d", rec.Code)
	}
}

func TestFollowupDisplayHintKeepsRealtimeHistoryRouting(t *testing.T) {
	gw := &busyGateway{}
	h := &SensingHandler{agentGateway: gw, monitorBus: monitor.ProvideBus(), config: &config.Config{}}
	calls := 0
	h.SetRealtimeHistory(func(string, string) (string, error) {
		calls++
		return "device-chat-context-followup", nil
	})
	rec := postRealtimeHandled(t, h, "voice_followup")
	if rec.Code != http.StatusOK || calls != 1 || gw.queued != 0 {
		t.Fatalf("display hint changed dispatch: status=%d history=%d queued=%d", rec.Code, calls, gw.queued)
	}
}

func TestVoiceTurnTypeIsValidatedDisplayMetadata(t *testing.T) {
	for _, tc := range []struct{ event, hint, want string }{
		{"voice_agent_handled", "voice_followup", "voice_followup"},
		{"voice_agent_handled", "invalid", ""},
		{"voice_followup", "", "voice_followup"},
		{"sound", "voice_command", ""},
	} {
		req := SensingEventRequest{Type: tc.event, VoiceTurnType: tc.hint}
		if got := req.voiceTurnType(); got != tc.want {
			t.Fatalf("%+v: got %q", tc, got)
		}
		if req.Type != tc.event {
			t.Fatal("display classification mutated routing")
		}
	}
}
