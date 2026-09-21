package http

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/intent"
	"go.autonomous.ai/os/system/intent/jev"
	"go.autonomous.ai/os/system/server/config"
)

type jevTestTransport func(*http.Request) (*http.Response, error)

func (f jevTestTransport) RoundTrip(req *http.Request) (*http.Response, error) { return f(req) }

func TestVoiceIntentWiresJevFlagKeyAndFallback(t *testing.T) {
	intent.Configure(map[string]bool{device.CapLight: true})
	t.Cleanup(func() { intent.Configure(nil) })

	previous := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = previous })
	providerCalls, halCalls := 0, 0
	http.DefaultTransport = jevTestTransport(func(req *http.Request) (*http.Response, error) {
		body := `{}`
		if req.URL.Host == "proxy.example.test" {
			providerCalls++
			if req.URL.Path != "/api/v1/ai/v1/jev/decisions" {
				t.Error("wrong BFF route")
			}
			if req.Header.Get("Authorization") != "Bearer test-jev-key" {
				t.Error("configured device key not used")
			}
			if _, ok := req.Context().Deadline(); !ok {
				t.Error("missing decision deadline")
			}
			body = `{"answers":{
			"intent":{"type":"choice","choice":"dim","probabilities":{"led_on":0.01,"led_off":0.01,"dim":0.97,"none":0.01}},
			"fit_led_on":{"type":"noul","noul":0.01},"fit_led_off":{"type":"noul","noul":0.01},"fit_dim":{"type":"noul","noul":0.99}}}`
		} else {
			halCalls++
			if req.URL.Path != "/led/solid" {
				t.Errorf("unexpected HAL action %s", req.URL.Path)
			}
		}
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(body))}, nil
	})
	enabled := false
	cfg := &config.Config{LLMBaseURL: "https://proxy.example.test/api/v1/ai/v1", LLMAPIKey: "test-jev-key", JevIntent: &config.JevIntentConfig{Enabled: &enabled}}
	h := &SensingHandler{config: cfg, intentResolver: jev.NewResolver()}
	if h.matchVoiceIntent(context.Background(), "ánh sáng chói quá") != nil || providerCalls != 0 || halCalls != 0 {
		t.Fatal("disabled path contacted provider or executed")
	}
	enabled = true
	got := h.matchVoiceIntent(context.Background(), "ánh sáng chói quá")
	if got == nil || got.Source != "jev" || got.Rule != "dim" || providerCalls != 1 || halCalls != 1 {
		t.Fatalf("enabled path failed: %+v, provider=%d HAL=%d", got, providerCalls, halCalls)
	}
	cfg.LLMAPIKey = ""
	if h.matchVoiceIntent(context.Background(), "ánh sáng chói quá") != nil || providerCalls != 1 || halCalls != 1 {
		t.Fatal("missing key did not fall through")
	}
}
