package internbridge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
)

func TestCassiFirstRoutes(t *testing.T) {
	for _, tc := range []struct {
		destination, intent, kind string
	}{
		{"orchestration@gus", "orchestration", "persona"},
		{"rex@dru", "engineering", "persona"},
		{"melvil@lab", "library", "persona"},
		{"pam@gus", "service", "service"},
		{"pam@gus", "notification", "service"},
		{"pam@gus", "alarm", "service"},
		{"pam@gus", "reminder", "service"},
		{"mcavoy@lab", "news", "service"},
		{"mcavoy@lab", "briefing", "service"},
	} {
		for _, op := range []Operation{Route, Reception, Classify, Generate} {
			t.Run(tc.destination+"/"+string(op), func(t *testing.T) {
				var calls atomic.Int32
				status := map[Operation]string{Route: "reception_route", Reception: "reception_route", Classify: "classified", Generate: "draft"}[op]
				if tc.kind == "service" {
					status = "service_route"
				}
				c := fixture(t, func(w http.ResponseWriter, r *http.Request) {
					calls.Add(1)
					b := result(status)
					b["requested_destination"], b["kind"] = tc.destination, tc.kind
					b["reception_route"] = reception(tc.destination, tc.intent)
					if status == "classified" {
						b["output"] = "action" // A label is never an executed action.
					}
					reply(w, 200, b)
				})
				req := request()
				req.Operation = op
				got, err := c.Do(context.Background(), req)
				if tc.kind == "service" {
					assertError(t, err, ErrServiceRoute)
					if got != nil {
						t.Fatal("service proposal exposed output or success")
					}
				} else if err != nil || got == nil || got.Destination != FirstContact || got.RequestedDestination != tc.destination ||
					got.Status != status || got.ReceptionRoute != (ReceptionRoute{FirstDestination: FirstContact, Handoff: tc.destination, Intent: tc.intent, Status: "reception_route", NextStep: "safe_escalation"}) {
					t.Fatalf("invalid reception metadata: result=%v error=%v", got, err)
				}
				if calls.Load() != 1 {
					t.Fatal("client retried or followed a proposed handoff")
				}
			})
		}
	}
}

func TestDeterministicUnknownServiceAdmission(t *testing.T) {
	for _, tc := range []struct {
		text string
		want error
	}{
		{"Gus, daily briefing", nil}, {"Gus, news headlines", nil},
		{"Gus, notify me", nil}, {"Gus, set an alarm", nil}, {"Gus, remind me", nil},
		{"Gus, news and turn on the Nanoleaf scene", ErrCustodyHold},
	} {
		err := ValidateRequest(Request{Text: tc.text, Operation: Reception, DataClass: Unknown})
		if !errors.Is(err, tc.want) {
			t.Fatalf("%q: %v", tc.text, err)
		}
	}
}

func TestMalformedReception(t *testing.T) {
	for _, tc := range []struct {
		name   string
		mutate func(map[string]any)
	}{
		{"schema", func(b map[string]any) { b["response_schema"] = "other" }},
		{"scope", func(b map[string]any) { b["lifecycle_scope"] = "employee" }},
		{"executor destination", func(b map[string]any) { b["destination"] = "rex@dru" }},
		{"unknown destination", func(b map[string]any) { b["requested_destination"] = "unknown@node" }},
		{"multiple destinations", func(b map[string]any) { b["requested_destination"] = []string{"rex@dru", "pam@gus"} }},
		{"joined destinations", func(b map[string]any) { b["requested_destination"] = "rex@dru,pam@gus" }},
		{"news is intent only", func(b map[string]any) { b["requested_destination"] = "news" }},
		{"bare mcavoy", func(b map[string]any) {
			b["requested_destination"], b["kind"], b["reception_route"] = "mcavoy", "service", reception("mcavoy", "news")
		}},
		{"wrong mcavoy node", func(b map[string]any) { b["requested_destination"] = "mcavoy@mama" }},
		{"route array", func(b map[string]any) { b["reception_route"] = []any{reception("rex@dru", "engineering")} }},
		{"route null", func(b map[string]any) { b["reception_route"] = nil }},
		{"route string", func(b map[string]any) { b["reception_route"] = "rex@dru" }},
		{"service fake draft", func(b map[string]any) {
			b["requested_destination"], b["kind"], b["reception_route"] = "mcavoy@lab", "service", reception("mcavoy@lab", "news")
		}},
		{"persona fake service", func(b map[string]any) { b["status"] = "service_route" }},
		{"reception not generation", func(b map[string]any) { b["status"] = "reception_route" }},
		{"output too long", func(b map[string]any) { b["output"] = strings.Repeat("a", MaxOutputChars+1) }},
	} {
		t.Run(tc.name, func(t *testing.T) { rejectResponse(t, tc.mutate) })
	}
	for _, field := range []string{"first_destination", "handoff", "intent", "status", "executed", "next_step"} {
		t.Run("missing nested/"+field, func(t *testing.T) {
			rejectResponse(t, func(b map[string]any) { delete(b["reception_route"].(map[string]any), field) })
		})
	}
	for _, tc := range []struct {
		key   string
		value any
	}{
		{"first_destination", "rex@dru"}, {"handoff", "rex@dru"}, {"handoff", ""},
		{"handoff", []string{"orchestration@gus", "rex@dru"}}, {"handoff", map[string]any{"destination": "rex@dru"}},
		{"handoff", "cassi@mama"}, {"intent", "unknown"}, {"intent", "unlisted"},
		{"status", "completed"}, {"executed", true}, {"executed", "false"}, {"executed", nil},
		{"next_step", "execute"}, {"handoffs", []string{"rex@dru"}}, {"extra", "private"},
	} {
		t.Run(fmt.Sprintf("nested/%s/%v", tc.key, tc.value), func(t *testing.T) {
			rejectResponse(t, func(b map[string]any) { b["reception_route"].(map[string]any)[tc.key] = tc.value })
		})
	}
	for field := range result("draft") {
		t.Run("missing top/"+field, func(t *testing.T) { rejectResponse(t, func(b map[string]any) { delete(b, field) }) })
		t.Run("null top/"+field, func(t *testing.T) { rejectResponse(t, func(b map[string]any) { b[field] = nil }) })
	}
}

func rejectResponse(t *testing.T, mutate func(map[string]any)) {
	t.Helper()
	c := fixture(t, func(w http.ResponseWriter, r *http.Request) { b := result("draft"); mutate(b); reply(w, 200, b) })
	got, err := c.Do(context.Background(), request())
	assertError(t, err, ErrProtocol)
	if got != nil {
		t.Fatal("invalid response exposed output")
	}
}

func TestReceptionDuplicateFields(t *testing.T) {
	for _, duplicate := range []string{`"handoff":"orchestration@gus",`, `"executed":false,`, `"\u0065xecuted":false,`} {
		c := fixture(t, func(w http.ResponseWriter, r *http.Request) {
			raw, _ := json.Marshal(result("draft"))
			body := strings.Replace(string(raw), `"reception_route":{`, `"reception_route":{`+duplicate, 1)
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, body)
		})
		got, err := c.Do(context.Background(), request())
		assertError(t, err, ErrProtocol)
		if got != nil {
			t.Fatal("duplicate field exposed output")
		}
	}
}

func TestCassiCustodyHolds(t *testing.T) {
	for _, dest := range []string{FirstContact, "smart-home"} {
		for _, op := range []Operation{Route, Reception, Classify, Generate} {
			for _, leak := range []bool{false, true} {
				c := fixture(t, func(w http.ResponseWriter, r *http.Request) {
					b := result("custody_hold")
					b["requested_destination"] = dest
					if dest == "smart-home" {
						b["kind"] = "service"
					}
					b["reception_route"] = reception(nil, "unknown")
					delete(b, "output")
					if leak {
						b["output"] = nil
					} // Even an explicit null is forbidden.
					reply(w, 200, b)
				})
				req := request()
				req.Operation = op
				got, err := c.Do(context.Background(), req)
				want := ErrCustodyHold
				if leak {
					want = ErrProtocol
				}
				assertError(t, err, want)
				if got != nil {
					t.Fatal("held response exposed content")
				}
			}
		}
	}
	// Cassi's common envelope must not make an arbitrary hold valid for public/business.
	rejectResponse(t, func(b map[string]any) {
		b["status"] = "custody_hold"
		delete(b, "output")
		b["reception_route"] = reception(nil, "unknown")
	})
	rejectResponse(t, func(b map[string]any) { b["status"] = "custody_hold"; delete(b, "output") })
	for _, dest := range []string{FirstContact, "smart-home"} {
		rejectResponse(t, func(b map[string]any) {
			b["requested_destination"] = dest
			if dest == "smart-home" {
				b["kind"] = "service"
				b["status"] = "service_route"
			}
			b["reception_route"] = reception(nil, "unknown")
		})
	}
}

func TestSafeEscalationWithoutHandoff(t *testing.T) {
	c := fixture(t, func(w http.ResponseWriter, r *http.Request) {
		b := result("reception_route")
		b["reception_route"] = reception(nil, "unknown")
		reply(w, 200, b)
	})
	r := request()
	r.Operation = Reception
	got, err := c.Do(context.Background(), r)
	if err != nil || got == nil || got.Status != "reception_route" || got.ReceptionRoute.Handoff != "" || got.ReceptionRoute.Executed {
		t.Fatalf("safe escalation changed into execution: result=%v error=%v", got, err)
	}
}

func TestLegacyRejected(t *testing.T) {
	for _, disguise := range []bool{false, true} {
		rejectResponse(t, func(b map[string]any) {
			b["version"] = "0.1.0"
			b["destination"] = "orchestration@gus"
			for _, field := range []string{"response_schema", "requested_destination", "reception_route", "lifecycle_scope"} {
				delete(b, field)
			}
			if disguise {
				b["version"] = Version
			}
		})
	}
	for _, path := range []string{"/health", "/ready", "/version"} {
		c := fixture(t, func(w http.ResponseWriter, r *http.Request) {
			s := "ok"
			if path == "/ready" {
				s = "ready"
			}
			reply(w, 200, map[string]any{"version": "0.1.0", "status": s, "transport": "loopback", "executes_actions": false})
		})
		assertError(t, c.probe(context.Background(), path, "ok"), ErrProtocol)
	}
}

func TestOutputCharacterBoundary(t *testing.T) {
	c := fixture(t, func(w http.ResponseWriter, r *http.Request) {
		b := result("draft")
		b["output"] = strings.Repeat("界", MaxOutputChars)
		reply(w, 200, b)
	})
	if _, err := c.Do(context.Background(), request()); err != nil {
		t.Fatal(err)
	}
}

func TestStrictErrorEnvelope(t *testing.T) {
	valid := func() map[string]any {
		return map[string]any{"version": Version, "response_schema": ResponseSchema, "run_id": "run-rejected", "destination": nil, "requested_destination": nil, "reception_route": nil, "kind": nil, "status": "rejected", "executes_actions": false, "transport_status": "rejected", "lifecycle_status": "not_started", "lifecycle_scope": "bridge_request", "error": "fixed test error"}
	}
	for _, status := range []int{400, 409, 413} {
		for key := range valid() {
			t.Run(fmt.Sprintf("%d/missing/%s", status, key), func(t *testing.T) {
				c := fixture(t, func(w http.ResponseWriter, r *http.Request) { b := valid(); delete(b, key); reply(w, status, b) })
				got, err := c.Do(context.Background(), request())
				assertError(t, err, ErrProtocol)
				if got != nil {
					t.Fatal("malformed error exposed result")
				}
			})
		}
		for _, key := range []string{"executes_actions", "output", "reception_route", "requested_destination", "lifecycle_scope", "extra"} {
			t.Run(fmt.Sprintf("%d/invalid/%s", status, key), func(t *testing.T) {
				c := fixture(t, func(w http.ResponseWriter, r *http.Request) { b := valid(); b[key] = true; reply(w, status, b) })
				got, err := c.Do(context.Background(), request())
				assertError(t, err, ErrProtocol)
				if got != nil {
					t.Fatal("invalid error exposed result")
				}
			})
		}
	}
}
