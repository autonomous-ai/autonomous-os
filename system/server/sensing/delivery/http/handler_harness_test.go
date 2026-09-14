package http

import (
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
)

func TestTruncateHarnessFollowupContextPreservesRuneBoundaries(t *testing.T) {
	text := strings.Repeat("é", maxHarnessFollowupContextRunes+1)
	got := truncateHarnessFollowupContext(text)
	if !strings.HasSuffix(got, "…") {
		t.Fatalf("truncated context = %q", got[len(got)-10:])
	}
	if len([]rune(got)) != maxHarnessFollowupContextRunes+1 {
		t.Fatalf("rune length = %d", len([]rune(got)))
	}
}

func TestHarnessVoiceBypassesLocalIntentAndDisconnectedMainRuntime(t *testing.T) {
	// A nil gateway panics if the handler consults main-runtime state. The
	// default config enables local intents, so this also checks route ordering.
	h := &SensingHandler{config: &config.Config{}, monitorBus: monitor.ProvideBus()}
	called := false
	h.SetHarnessVoice(func(c *gin.Context, req SensingEventRequest) bool {
		called = true
		if req.Message != "what time is it" || req.HarnessVoice == nil || !req.HarnessVoice.Enabled {
			t.Fatalf("direct input changed: %+v", req)
		}
		c.JSON(200, gin.H{"runId": "direct-voice"})
		return true
	})
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest("POST", "/api/sensing/event", strings.NewReader(
		`{"type":"voice_command","message":"what time is it","harness_voice":{"enabled":true,"generation":42}}`))
	c.Request.Header.Set("Content-Type", "application/json")
	h.PostEvent(c)
	if !called || w.Code != 200 || !strings.Contains(w.Body.String(), "direct-voice") {
		t.Fatalf("direct route not taken: %s", w.Body.String())
	}
}
