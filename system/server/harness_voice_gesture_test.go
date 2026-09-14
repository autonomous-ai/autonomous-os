package server

import (
	"context"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/harness"
)

func TestHarnessVoiceGestureLocalAuthAndDeduplication(t *testing.T) {
	voice := harness.NewVoiceController(&voiceRouteTransport{}, harness.VoiceCallbacks{})
	s := &Server{harnessVoice: voice}
	router := gin.New()
	s.registerHarnessVoiceRoutes(router.Group("/api/harness"))
	const payload = `{"gestureId":"07c61377-971e-47a9-8742-e0f72d441955"}`
	call := func(address, forwarded, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest("POST", "/api/harness/voice-mode/gesture", strings.NewReader(body))
		req.RemoteAddr = address
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-Forwarded-For", forwarded)
		out := httptest.NewRecorder()
		router.ServeHTTP(out, req)
		return out
	}
	for _, tc := range []struct{ address, forwarded string }{
		{"192.168.1.2:50", ""}, {"127.0.0.1:50", "192.168.1.2"},
	} {
		if out := call(tc.address, tc.forwarded, payload); out.Code != 403 {
			t.Fatalf("remote allowed: %d", out.Code)
		}
	}
	for _, body := range []string{`{}`, `{"gestureId":""}`, `{"gestureId":"not-a-uuid"}`} {
		if out := call("127.0.0.1:50", "", body); out.Code != 400 {
			t.Fatalf("invalid ID allowed: %d", out.Code)
		}
	}
	for range 2 {
		out := call("127.0.0.1:50", "", payload)
		if out.Code != 200 || !voice.State().Enabled || !strings.Contains(out.Body.String(), `"agentName":"Mike"`) {
			t.Fatalf("gesture: %d %s", out.Code, out.Body.String())
		}
	}
	_, _ = voice.SetMode(context.Background(), false)
	out := call("127.0.0.1:50", "", payload)
	if out.Code != 200 || voice.State().Enabled {
		t.Fatal("old HTTP retry toggled after explicit off")
	}
}
