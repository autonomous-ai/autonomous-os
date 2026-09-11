package environment

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"go.autonomous.ai/os/system/lib/sensingmsg"
	"go.autonomous.ai/os/system/server/config"
)

func TestHTTPSenderAcceptanceContract(t *testing.T) {
	for _, tc := range []struct {
		name, body          string
		code                int
		accepted, wantError bool
	}{
		{"live", `{"status":1,"data":{"runId":"run-1"}}`, 200, true, false},
		{"queued", `{"status":1,"data":{"handler":"queued"}}`, 200, true, false},
		{"floor", `{"status":1,"data":{"handler":"dropped_floor"}}`, 200, false, false},
		{"sleep", `{"status":1,"data":{"handler":"dropped_sleeping"}}`, 200, false, false},
		{"busy drop", `{"status":1,"data":{"handler":"dropped"}}`, 200, false, false},
		{"failure envelope", `{"status":0,"data":null}`, 200, false, false},
		{"empty acknowledgement", `{"status":1,"data":{}}`, 200, false, false},
		{"invalid JSON", `{`, 200, false, true},
		{"forbidden", `{}`, 403, false, true},
		{"server failure", `{}`, 500, false, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodPost || r.Header.Get("Content-Type") != "application/json" {
					t.Error("invalid request metadata")
				}
				var body map[string]string
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if body["type"] != "environment.update" || !strings.Contains(body["message"], `"observed_at":1000`) {
					t.Errorf("invalid event body: %#v", body)
				}
				var event Event
				if err := json.Unmarshal([]byte(body["message"]), &event); err != nil {
					t.Errorf("message must be raw event JSON: %v", err)
				}
				built := sensingmsg.Build(body["type"], body["message"], "", "")
				if strings.Count(built, "[environment:update]") != 1 {
					t.Errorf("duplicate or missing agent prefix: %s", built)
				}
				w.WriteHeader(tc.code)
				_, _ = w.Write([]byte(tc.body))
			}))
			defer server.Close()
			accepted, err := HTTPSender(server.URL)(context.Background(), Event{ObservedAt: 1000})
			if accepted != tc.accepted || (err != nil) != tc.wantError {
				t.Fatalf("accepted=%v err=%v", accepted, err)
			}
		})
	}
}
func TestHTTPSenderHonorsCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	accepted, err := HTTPSender("http://127.0.0.1:1")(ctx, Event{})
	if accepted || !errors.Is(err, context.Canceled) {
		t.Fatalf("accepted=%v err=%v", accepted, err)
	}
}
func TestServiceDoesNotReadWithoutEnableAndCapability(t *testing.T) {
	for _, tc := range []struct {
		name               string
		enabled, available bool
	}{{"disabled", false, true}, {"capability absent", true, false}} {
		t.Run(tc.name, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			settings := testSettings()
			settings.Enabled = tc.enabled
			polled := make(chan struct{})
			done := make(chan struct{})
			var reads atomic.Int32
			service := Service{Settings: func() config.EnvironmentConfig { close(polled); return settings }, Available: func() bool { return tc.available }, Read: func(context.Context) (json.RawMessage, error) { reads.Add(1); return nil, nil }}
			go func() { service.Run(ctx); close(done) }()
			select {
			case <-polled:
			case <-time.After(time.Second):
				t.Fatal("no initial settings read")
			}
			cancel()
			select {
			case <-done:
			case <-time.After(time.Second):
				t.Fatal("cancellation failed")
			}
			if reads.Load() != 0 {
				t.Fatal("HAL polled without enabled capability")
			}
		})
	}
}
func TestServiceCancelsInFlightRead(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	started := make(chan struct{})
	done := make(chan struct{})
	s := Service{Settings: testSettings, Available: func() bool { return true }, Read: func(ctx context.Context) (json.RawMessage, error) {
		close(started)
		<-ctx.Done()
		return nil, ctx.Err()
	}}
	go func() { s.Run(ctx); close(done) }()
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("read did not start")
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("read or timer ignored cancellation")
	}
}
func TestServiceReadErrorResetsBaselineBeforeRecovery(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	c := testSettings()
	c.EvaluateIntervalS = 1
	c.SustainS = 1
	c.RetryIntervalS = 1
	c.Metrics["temperature_c"] = config.EnvironmentMetricRule{Delta: 2, WarmupS: 0}
	count := 0
	sent := make(chan Event, 1)
	done := make(chan struct{})
	s := Service{Settings: func() config.EnvironmentConfig { return c }, Available: func() bool { return true }, Read: func(context.Context) (json.RawMessage, error) {
		count++
		if count == 3 {
			return nil, errors.New("sensor disconnected")
		}
		value := 20.0
		if count >= 2 {
			value = 23
		}
		if count >= 5 {
			value = 26
		}
		return json.Marshal(sample(count, value))
	}, Send: func(_ context.Context, event Event) (bool, error) { sent <- event; cancel(); return true, nil }}
	go func() { s.Run(ctx); close(done) }()
	select {
	case event := <-sent:
		mustChange(t, &event, 23, 26)
	case <-time.After(8 * time.Second):
		t.Fatal("no sustained recovery event")
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("worker did not stop")
	}
}
