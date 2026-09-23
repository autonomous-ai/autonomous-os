package intent

import (
	"context"
	"net/http"
	"testing"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/intent/jev"
)

func TestContextualCommandsBypassLocalExecution(t *testing.T) {
	phrases := []string{
		"don't turn off the light", "[do not] light off", "speaker says: light off", "do not volume up", "volume up to 20%",
		"volume down by ten percent", "turn off the light in the bedroom",
		"volume down and light off", `"light off"`, "what time is my flight",
		"what time is it in Tokyo", "why is reading mode enabled", "track the cup and the dog",
		"do not follow me", "set the light red tomorrow", "tell me how to turn off the light",
		"[voice-instruction] do not turn off the light\n[transcript] light off",
	}
	for _, text := range phrases {
		t.Run(text, func(t *testing.T) {
			routeIntentHAL(t, func(w http.ResponseWriter, r *http.Request) { t.Errorf("unexpected HAL call: %s", r.URL.Path) })
			Configure(map[string]bool{device.CapLight: true, device.CapAudio: true, device.CapMotion: true})
			called := false
			resolver := fakeJevSelectionResolver(func([]jev.Candidate) jev.Selection { called = true; return jev.Selection{} })
			got := MatchWithFallback(context.Background(), text, resolver, jev.Options{Enabled: true, APIKey: "test", Endpoint: "https://example.test"})
			if got != nil || !called {
				t.Fatalf("result=%+v resolver called=%v", got, called)
			}
			called = false
			got = MatchWithFallback(context.Background(), text, resolver, jev.Options{})
			if got != nil || called {
				t.Fatalf("disabled result=%+v resolver called=%v", got, called)
			}
		})
	}
}

func TestCanonicalCommandsStayLocal(t *testing.T) {
	for _, text := range []string{"light off", "Please turn off the light!", "set the light purple.", "track the cup", "follow me", "stop tracking", "reading mode", "deactivate focus mode", "what time is it?"} {
		t.Run(text, func(t *testing.T) {
			routeIntentHAL(t, func(w http.ResponseWriter, r *http.Request) {})
			resolver := fakeJevSelectionResolver(func([]jev.Candidate) jev.Selection {
				t.Fatal("canonical command contacted resolver")
				return jev.Selection{}
			})
			got := MatchWithFallback(context.Background(), text, resolver, jev.Options{Enabled: true, APIKey: "test", Endpoint: "https://example.test"})
			if got == nil || got.Source == "jev" || got.ExecutionFailed {
				t.Fatalf("result=%+v", got)
			}
		})
	}
}
