package intent

import (
	"context"
	"net/http"
	"reflect"
	"sync/atomic"
	"testing"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/intent/jev"
)

type fakeJevDecider func(context.Context, string, string, []jev.Candidate) (string, error)

func (f fakeJevDecider) Resolve(ctx context.Context, text string, candidates []jev.Candidate, options jev.Options) string {
	id, _ := f(ctx, options.APIKey, text, candidates)
	return id
}

func configureJevTest(t *testing.T) *atomic.Int32 {
	t.Helper()
	calls := &atomic.Int32{}
	routeIntentHAL(t, func(w http.ResponseWriter, r *http.Request) { calls.Add(1) })
	Configure(map[string]bool{device.CapLight: true, device.CapAudio: true})
	return calls
}

func TestJevLocalDisabledAndMissingKeyNeverCallProvider(t *testing.T) {
	configureJevTest(t)
	r := fakeJevDecider(func(context.Context, string, string, []jev.Candidate) (string, error) {
		t.Fatal("fast path contacted provider")
		return "", nil
	})
	for _, tc := range []struct {
		text string
		opts jev.Options
		want string
	}{
		{"light off", jev.Options{Endpoint: "https://proxy.example.test/jev/decisions", Enabled: true, APIKey: "test"}, "led_off"},
		{"ánh sáng chói quá", jev.Options{Endpoint: "https://proxy.example.test/jev/decisions", APIKey: "test"}, ""},
		{"ánh sáng chói quá", jev.Options{Endpoint: "https://proxy.example.test/jev/decisions", Enabled: true}, ""},
	} {
		got := MatchWithFallback(context.Background(), tc.text, r, tc.opts)
		if tc.want == "" && got != nil || tc.want != "" && (got == nil || got.Rule != tc.want || got.Source != "") {
			t.Fatalf("Match(%q) = %+v", tc.text, got)
		}
	}
}

func TestJevRecognitionHasNoEffectsAndExecutionUsesExistingRule(t *testing.T) {
	calls := configureJevTest(t)
	command := recognizeCommand("turn off the light")
	if command == nil || command.rule.name != "led_off" || calls.Load() != 0 {
		t.Fatal("recognition executed hardware or lost rule")
	}
	r := fakeJevDecider(func(ctx context.Context, key, text string, candidates []jev.Candidate) (string, error) {
		if calls.Load() != 0 || text != "ánh sáng chói quá" || key != "test" {
			t.Fatal("unexpected inference input or early side effect")
		}
		var ids []string
		for _, c := range candidates {
			ids = append(ids, c.ID)
		}
		if !reflect.DeepEqual(ids, []string{"led_on", "led_off", "dim", "volume_up", "volume_down"}) {
			t.Fatalf("unexpected catalogue: %v", ids)
		}
		return "dim", nil
	})
	got := MatchWithFallback(context.Background(), "ánh sáng chói quá", r, jev.Options{Endpoint: "https://proxy.example.test/jev/decisions", Enabled: true, APIKey: "test"})
	if got == nil || got.Rule != "dim" || got.Source != "jev" || calls.Load() != 1 || !got.LEDChanged {
		t.Fatalf("semantic result = %+v, HAL calls=%d", got, calls.Load())
	}
	if !reflect.DeepEqual(got.Actions, []string{`POST /led/solid {"color":[80,60,40]}`}) {
		t.Fatalf("unexpected hardware payload: %v", got.Actions)
	}
}

func TestJevCannotExecuteUnofferedOrRemovedCapability(t *testing.T) {
	for _, id := range []string{"scene_night", "led_off", "made_up_action", ""} {
		t.Run(id, func(t *testing.T) {
			calls := configureJevTest(t)
			Configure(map[string]bool{device.CapAudio: true})
			r := fakeJevDecider(func(context.Context, string, string, []jev.Candidate) (string, error) { return id, nil })
			if got := MatchWithFallback(context.Background(), "ánh sáng chói quá", r, jev.Options{Endpoint: "https://proxy.example.test/jev/decisions", Enabled: true, APIKey: "test"}); got != nil || calls.Load() != 0 {
				t.Fatalf("unoffered action executed: %+v", got)
			}
		})
	}
	t.Run("capability changed during request", func(t *testing.T) {
		calls := configureJevTest(t)
		r := fakeJevDecider(func(context.Context, string, string, []jev.Candidate) (string, error) {
			Configure(map[string]bool{device.CapAudio: true})
			return "led_off", nil
		})
		if MatchWithFallback(context.Background(), "ánh sáng chói quá", r, jev.Options{Endpoint: "https://proxy.example.test/jev/decisions", Enabled: true, APIKey: "test"}) != nil || calls.Load() != 0 {
			t.Fatal("removed capability executed")
		}
	})
}

func TestJevExecutionFailureRemainsHandled(t *testing.T) {
	routeIntentHAL(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusBadGateway) })
	Configure(map[string]bool{device.CapLight: true})
	r := fakeJevDecider(func(context.Context, string, string, []jev.Candidate) (string, error) { return "dim", nil })
	got := MatchWithFallback(context.Background(), "ánh sáng chói quá", r, jev.Options{Endpoint: "https://proxy.example.test/jev/decisions", Enabled: true, APIKey: "test"})
	if got == nil || !got.ExecutionFailed || got.Source != "jev" {
		t.Fatalf("lost failed execution: %+v", got)
	}
}
