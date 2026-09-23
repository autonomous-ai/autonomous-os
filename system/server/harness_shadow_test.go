package server

import (
	"context"
	"encoding/json"
	"reflect"
	"testing"
	"time"

	"go.autonomous.ai/os/system/harness"
	"go.autonomous.ai/os/system/intent/jev"
	"go.autonomous.ai/os/system/server/config"
)

type shadowResolveFunc func(context.Context, string, []jev.Candidate, jev.Options) jev.Selection

func (f shadowResolveFunc) Resolve(ctx context.Context, text string, candidates []jev.Candidate, options jev.Options) jev.Selection {
	return f(ctx, text, candidates, options)
}

func shadowFixture(t *testing.T) (*harnessShadow, harness.Status, harness.Frame, config.JevIntentSettings) {
	t.Helper()
	s := newHarnessShadow()
	status := harness.Status{Connected: true, MachineID: "mac", ServerInstanceID: "daemon"}
	var listing harness.Frame
	if err := json.Unmarshal([]byte(`{"machineId":"mac","agents":[{"agentId":"airplane","recap":"Created airplane","workspace":{"path":"/airplane"}},{"agentId":"house","recap":"Created house with garden","workspace":{"path":"/house"}}]}`), &listing); err != nil {
		t.Fatal(err)
	}
	s.remember(listing, nil, status)
	return s, status, harness.Frame{"type": "turn.send", "machineId": "mac", "agentId": "airplane", "text": "Add trees to the house garden", "idempotencyKey": "unchanged"}, config.JevIntentSettings{Enabled: true, Endpoint: "https://proxy.test/jev/decisions", APIKey: "test", TimeoutMS: 100}
}

func TestHarnessShadowDisagreementDoesNotChangeDispatch(t *testing.T) {
	s, status, frame, settings := shadowFixture(t)
	original := harness.Frame{}
	for k, v := range frame {
		original[k] = v
	}
	done := make(chan string, 1)
	s.report = func(outcome, actual, proposed string, _ int64) { done <- outcome + ":" + actual + ":" + proposed }
	s.resolver = shadowResolveFunc(func(_ context.Context, text string, candidates []jev.Candidate, _ jev.Options) jev.Selection {
		if text != frame["text"] || len(candidates) != 2 {
			t.Error("lost task evidence")
		}
		return jev.Selection{Intent: "session_1"}
	})
	s.compare(context.Background(), frame, status, settings)
	select {
	case got := <-done:
		if got != "disagree:airplane:house" {
			t.Fatal(got)
		}
	case <-time.After(time.Second):
		t.Fatal("shadow did not finish")
	}
	if !reflect.DeepEqual(frame, original) {
		t.Fatal("shadow modified dispatch")
	}
}

func TestHarnessShadowSkipsUnsafeSnapshotsAndDisabled(t *testing.T) {
	for _, name := range []string{"disabled", "missing_config", "stale", "restart", "offline", "absent_target", "oversized_list", "oversized_metadata", "failed_list"} {
		t.Run(name, func(t *testing.T) {
			s, status, frame, settings := shadowFixture(t)
			s.resolver = shadowResolveFunc(func(context.Context, string, []jev.Candidate, jev.Options) jev.Selection {
				t.Error("unexpected provider call")
				return jev.Selection{}
			})
			outcomes := make(chan string, 1)
			s.report = func(outcome, _, _ string, _ int64) { outcomes <- outcome }
			switch name {
			case "disabled":
				settings.Enabled = false
			case "missing_config":
				settings.APIKey = ""
			case "stale":
				s.captured = time.Now().Add(-31 * time.Second)
			case "restart":
				status.ServerInstanceID = "new"
			case "offline":
				status.Connected = false
			case "absent_target":
				frame["agentId"] = "missing"
			case "oversized_list":
				s.remember(harness.Frame{"machineId": "mac", "agents": make([]any, 33)}, nil, status)
			case "oversized_metadata":
				s.remember(harness.Frame{"machineId": "mac", "agents": []any{map[string]any{"agentId": "airplane", "recap": string(make([]byte, 1001))}}}, nil, status)
			case "failed_list":
				s.remember(harness.Frame{"error": "offline"}, nil, status)
			}
			s.compare(context.Background(), frame, status, settings)
			if s.busy.Load() {
				t.Fatal("unexpected worker")
			}
			if name != "disabled" {
				select {
				case <-outcomes:
				default:
					t.Fatal("missing skip diagnostic")
				}
			}
		})
	}
}

func TestHarnessShadowWorkerDoesNotBlockAndCancels(t *testing.T) {
	s, status, frame, settings := shadowFixture(t)
	settings.TimeoutMS = 3000
	entered, exited := make(chan struct{}), make(chan struct{})
	outcomes := make(chan string, 2)
	s.report = func(outcome, _, _ string, _ int64) { outcomes <- outcome }
	s.resolver = shadowResolveFunc(func(ctx context.Context, _ string, _ []jev.Candidate, _ jev.Options) jev.Selection {
		close(entered)
		<-ctx.Done()
		close(exited)
		return jev.Selection{Intent: "session_1"}
	})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	returned := make(chan struct{})
	go func() { s.compare(ctx, frame, status, settings); close(returned) }()
	select {
	case <-returned:
	case <-time.After(time.Second):
		t.Fatal("dispatch blocked by shadow")
	}
	<-entered
	s.compare(ctx, frame, status, settings)
	if got := <-outcomes; got != "skip_busy" {
		t.Fatal(got)
	}
	cancel()
	select {
	case <-exited:
	case <-time.After(time.Second):
		t.Fatal("worker ignored cancellation")
	}
	if got := <-outcomes; got != "abstain" {
		t.Fatal(got)
	}
}

func TestHarnessShadowRejectsUnlistedChoice(t *testing.T) {
	s, status, frame, settings := shadowFixture(t)
	done := make(chan string, 1)
	s.report = func(outcome, _, _ string, _ int64) { done <- outcome }
	s.resolver = shadowResolveFunc(func(context.Context, string, []jev.Candidate, jev.Options) jev.Selection {
		return jev.Selection{Intent: "invented"}
	})
	s.compare(context.Background(), frame, status, settings)
	select {
	case got := <-done:
		if got != "abstain" {
			t.Fatal(got)
		}
	case <-time.After(time.Second):
		t.Fatal("worker stalled")
	}
}
