package internbridge

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestCustodyAdmissionBeforeNetwork(t *testing.T) {
	var calls atomic.Int32
	c := fixture(t, func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		reply(w, 200, result("draft"))
	})
	for _, class := range []DataClass{Unknown, Restricted, Secret} {
		for _, op := range []Operation{Route, Reception, Classify, Generate} {
			t.Run(string(class)+"/"+string(op), func(t *testing.T) {
				const sensitive = "private-content-must-not-leave"
				got, err := c.Do(context.Background(), Request{Text: sensitive, Operation: op, DataClass: class})
				want := ErrCustodyHold
				if class == Unknown {
					want = ErrNeedsClassification
				}
				assertError(t, err, want)
				var safe *Error
				errors.As(err, &safe)
				if got != nil || safe.HTTPStatus != 0 || strings.Contains(err.Error(), sensitive) {
					t.Fatal("local custody rejection leaked data or claimed an HTTP response")
				}
			})
		}
	}
	if calls.Load() != 0 {
		t.Fatal("held request reached the listener")
	}
	// Positive control: the same client/listener really accepts admitted work.
	for _, class := range []DataClass{Public, Business} {
		r := request()
		r.DataClass = class
		if _, err := c.Do(context.Background(), r); err != nil {
			t.Fatal(err)
		}
	}
	if calls.Load() != 2 {
		t.Fatal("admitted requests did not reach listener")
	}
}

func TestProbeGeneration(t *testing.T) {
	for _, tc := range []struct {
		status string
		want   error
	}{
		{"draft", nil}, {"fallback", ErrUnavailable},
		{"reception_route", ErrProtocol}, {"classified", ErrProtocol},
		{"service_route", ErrServiceRoute}, {"needs_input", ErrNeedsInput},
	} {
		t.Run(tc.status, func(t *testing.T) {
			var probes, generations atomic.Int32
			c := fixture(t, func(w http.ResponseWriter, r *http.Request) {
				if r.Method == "GET" && r.URL.Path == "/ready" {
					probes.Add(1)
					reply(w, 200, map[string]any{"version": Version, "response_schema": ResponseSchema, "status": "ready", "transport": "loopback", "executes_actions": false})
					return
				}
				generations.Add(1)
				var req Request
				if r.Method != "POST" || r.URL.Path != "/v1/intern" || json.NewDecoder(r.Body).Decode(&req) != nil ||
					req.Text != "Write a brief greeting." || req.DataClass != Public || req.Operation != Generate || req.RunID != "" {
					t.Error("probe did not use fixed public generation request")
				}
				b := result(tc.status)
				if tc.status == "service_route" {
					b["requested_destination"], b["kind"] = "pam@gus", "service"
					b["reception_route"] = reception("pam@gus", "service")
				}
				reply(w, 200, b)
			})
			// Repeat proves there is no cached success or reused idempotency key.
			for i := 0; i < 2; i++ {
				err := c.ProbeGeneration(context.Background())
				if tc.want == nil {
					if err != nil {
						t.Fatal(err)
					}
				} else {
					assertError(t, err, tc.want)
				}
			}
			if probes.Load() != 2 || generations.Load() != 2 {
				t.Fatal("probe skipped a stage or retried")
			}
		})
	}
}

func TestProbeGenerationStopsOnMetadataFailure(t *testing.T) {
	var calls atomic.Int32
	c := fixture(t, func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Method != "GET" {
			t.Error("generation sent after failed readiness")
		}
		reply(w, 200, map[string]any{"version": "unsupported"})
	})
	assertError(t, c.ProbeGeneration(context.Background()), ErrProtocol)
	assertError(t, c.ProbeGeneration(nil), ErrInvalidRequest)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	assertError(t, c.ProbeGeneration(ctx), ErrCanceled)
	if calls.Load() != 1 {
		t.Fatal("invalid/canceled probe sent a request")
	}
}

func TestProbeGenerationDeadline(t *testing.T) {
	var generations atomic.Int32
	c := fixture(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "GET" {
			reply(w, 200, map[string]any{"version": Version, "response_schema": ResponseSchema, "status": "ready", "transport": "loopback", "executes_actions": false})
			return
		}
		generations.Add(1)
		select {
		case <-r.Context().Done():
		case <-time.After(200 * time.Millisecond):
		}
	})
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	assertError(t, c.ProbeGeneration(ctx), ErrDeadline)
	if generations.Load() != 1 {
		t.Fatal("generation not attempted exactly once")
	}
}
