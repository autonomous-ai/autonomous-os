package bridge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"go.autonomous.ai/os/system/lib/internbridge"
)

func TestReceptionLocalClassification(t *testing.T) {
	for _, tc := range []struct {
		name, input, label, destination, kind, intent, status string
		calls                                                 int32
	}{
		{"briefing", "Give me a rundown to start the day.", "briefing", "mcavoy@lab", "service", "briefing", "service_route", 1},
		{"news", "What happened in the world today?", "news", "mcavoy@lab", "service", "news", "service_route", 1},
		{"alarm", "Gus, wake me at seven.", "alarm", "pam@gus", "service", "alarm", "service_route", 1},
		{"notification", "Let me know when the report is ready.", "notification", "pam@gus", "service", "notification", "service_route", 1},
		{"reminder", "Help me remember to submit the report.", "reminder", "pam@gus", "service", "reminder", "service_route", 1},
		{"engineering", "Help fix a compiler error.", "engineering", "rex@dru", "persona", "engineering", "reception_route", 1},
		{"library", "Find a reference about sorting algorithms.", "library", "melvil@lab", "persona", "library", "reception_route", 1},
		{"orchestration", "Help prioritize the project work.", "orchestration", "orchestration@gus", "persona", "orchestration", "reception_route", 1},
		{"service", "Help with an operational request.", "service", "pam@gus", "service", "service", "service_route", 1},
		{"classified home", "Adjust the thermostat.", "smart-home", "smart-home", "service", "smart-home", "custody_hold", 1},
		{"classified reception", "I need the receptionist.", "reception", internbridge.FirstContact, "persona", "reception", "custody_hold", 1},
		{"unknown", "Help with that thing.", "unknown", "orchestration@gus", "persona", "unknown", "reception_route", 1},
		{"multiple destinations", "Fix a compiler error and find a reference.", "unknown", "orchestration@gus", "persona", "unknown", "reception_route", 1},
		{"named Rex", "Hey Gus, Rex, help with the build.", "news", "rex@dru", "persona", "engineering", "reception_route", 0},
		{"named Melvil", "Melvil, find a reference.", "alarm", "melvil@lab", "persona", "library", "reception_route", 0},
		{"named PAM", "PAM, help with a request.", "news", "pam@gus", "service", "service", "service_route", 0},
		{"named Cassi", "Gus, Cassi, help.", "news", internbridge.FirstContact, "persona", "reception", "custody_hold", 0},
		{"home preflight", "Turn on the Hue light.", "news", "smart-home", "service", "smart-home", "custody_hold", 0},
		{"mixed home", "Give me a briefing and turn on the fan.", "briefing", "smart-home", "service", "smart-home", "custody_hold", 0},
		{"deterministic briefing", "Give me a morning briefing.", "alarm", "mcavoy@lab", "service", "briefing", "service_route", 0},
		{"deterministic alarm", "Set an alarm for seven.", "news", "pam@gus", "service", "alarm", "service_route", 0},
		{"mixed services", "Give me news and set an alarm.", "news", "orchestration@gus", "persona", "unknown", "reception_route", 0},
		{"leading mixed services", "News and an alarm please.", "news", "orchestration@gus", "persona", "unknown", "reception_route", 0},
		{"empty wake", "Gus", "news", "orchestration@gus", "persona", "unknown", "needs_input", 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var calls atomic.Int32
			p := testProvider(t, "ollama", func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				var b map[string]any
				if err := json.NewDecoder(r.Body).Decode(&b); err != nil {
					t.Error(err)
					return
				}
				messages := b["messages"].([]any)
				instruction := messages[0].(map[string]any)["content"].(string)
				if len(messages) != 2 || messages[1].(map[string]any)["content"] != tc.input ||
					!strings.Contains(instruction, "Welcome Desk") || !strings.Contains(instruction, "Ambiguous or multiple destinations mean unknown") ||
					b["think"] != false || b["stream"] != false || b["keep_alive"] != "5m" ||
					b["options"].(map[string]any)["num_predict"] != float64(256) || b["options"].(map[string]any)["temperature"] != float64(0) ||
					r.Header.Get("Authorization") != "" {
					t.Error("unbounded or incorrect classification payload")
				}
				w.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(w).Encode(ollamaReply(tc.label))
			})
			h := newHandler(p)
			req := internbridge.Request{Text: tc.input, Operation: internbridge.Reception, DataClass: internbridge.Business}
			raw, _ := json.Marshal(req)
			w := rawRequest(h, string(raw))
			var got response
			if w.Code != 200 || json.Unmarshal(w.Body.Bytes(), &got) != nil {
				t.Fatalf("bad response: %s", w.Body.String())
			}
			if got.Destination != internbridge.FirstContact || got.Reception.FirstDestination != internbridge.FirstContact ||
				got.RequestedDestination != tc.destination || got.Kind != tc.kind || got.Status != tc.status || got.Reception.Intent != tc.intent ||
				got.Reception.Executed || got.ExecutesActions || got.Reception.NextStep != "safe_escalation" ||
				got.LifecycleScope != "bridge_request" || got.LifecycleStatus != "completed" || calls.Load() != tc.calls {
				t.Fatalf("unexpected proposal: %+v, calls=%d", got, calls.Load())
			}
			if tc.status == "custody_hold" || tc.intent == "unknown" {
				if got.Reception.Handoff != nil || (tc.status == "custody_hold" && got.Output != nil) {
					t.Fatal("hold/clarification made a proposal or custody emitted output")
				}
			} else if got.Reception.Handoff == nil || *got.Reception.Handoff != tc.destination {
				t.Fatal("missing single proposal")
			}
			// Verify the existing consumer understands the unchanged wire contract.
			var want error
			switch tc.status {
			case "custody_hold":
				want = internbridge.ErrCustodyHold
			case "service_route":
				want = internbridge.ErrServiceRoute
			case "needs_input":
				want = internbridge.ErrNeedsInput
			}
			_, err := bridgeFixture(t, h).Do(context.Background(), req)
			if !errors.Is(err, want) || calls.Load() != 2*tc.calls {
				t.Fatalf("consumer error=%v want=%v calls=%d", err, want, calls.Load())
			}
		})
	}
}

func TestReceptionRejectsMalformedAndFailedClassification(t *testing.T) {
	for _, mode := range []string{"unknown", "news, alarm", "news\nalarm", `{"intent":"news"}`, "News", "mcavoy@lab", "question", "news because private", "<think>private</think>news", "", "http-error", "bad-json", "thinking", "token-burn", "truncated", "redirect"} {
		t.Run(mode, func(t *testing.T) {
			var calls atomic.Int32
			p := testProvider(t, "ollama", func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				w.Header().Set("Content-Type", "application/json")
				b := ollamaReply(mode)
				switch mode {
				case "http-error":
					w.WriteHeader(503)
					fmt.Fprint(w, "private provider error")
					return
				case "bad-json":
					fmt.Fprint(w, "private invalid JSON")
					return
				case "redirect":
					w.Header().Set("Location", "/private")
					w.WriteHeader(307)
					return
				case "thinking":
					b = ollamaReply("news")
					b["message"].(map[string]any)["thinking"] = "private"
				case "token-burn":
					b = ollamaReply("news")
					b["eval_count"] = 200
				case "truncated":
					b = ollamaReply("news")
					b["done_reason"] = "length"
				}
				_ = json.NewEncoder(w).Encode(b)
			})
			w := rawRequest(newHandler(p), `{"text":"What happened today?","operation":"reception","data_class":"public"}`)
			var got response
			if json.Unmarshal(w.Body.Bytes(), &got) != nil || got.Status != "reception_route" || got.Reception.Intent != "unknown" ||
				got.Reception.Handoff != nil || got.Output == nil || *got.Output != "Could you clarify?" ||
				strings.Contains(w.Body.String(), "private") || calls.Load() != 1 {
				t.Fatalf("unsafe classification failure: %s calls=%d", w.Body.String(), calls.Load())
			}
		})
	}
}

func TestReceptionProviderPreflightAndCerebras(t *testing.T) {
	for _, kind := range []string{"ollama", "cerebras"} {
		t.Run(kind, func(t *testing.T) {
			var calls atomic.Int32
			p := testProvider(t, kind, func(w http.ResponseWriter, r *http.Request) { calls.Add(1); t.Error("preflight reached network") })
			for _, tc := range []struct {
				input string
				class internbridge.DataClass
			}{
				{"private sentinel", internbridge.Restricted}, {"private sentinel", internbridge.Secret},
				{"What happened today?", internbridge.Unknown}, {"Give me news.", internbridge.Unknown},
				{"Turn on the Hue light.", internbridge.Public}, {"Gus Cassi, hello.", internbridge.Business},
			} {
				req := internbridge.Request{Text: tc.input, Operation: internbridge.Reception, DataClass: tc.class}
				if output, err := p.Complete(context.Background(), req); err == nil || output != "" {
					t.Fatal("direct provider bypassed custody/admission")
				}
				raw, _ := json.Marshal(req)
				w := rawRequest(newHandler(p), string(raw))
				if w.Code != 200 || strings.Contains(w.Body.String(), "private sentinel") {
					t.Fatal("bridge preflight leaked input")
				}
			}
			if kind == "cerebras" {
				w := rawRequest(newHandler(p), `{"text":"What happened today?","operation":"reception","data_class":"public"}`)
				var got response
				if json.Unmarshal(w.Body.Bytes(), &got) != nil || got.Reception.Handoff != nil || got.Reception.Intent != "unknown" || got.Output == nil || *got.Output != "Could you clarify?" {
					t.Fatal("Cerebras reception must clarify without inference or alternate provider")
				}
			}
			if calls.Load() != 0 {
				t.Fatal("preflight performed inference")
			}
		})
	}
}

func TestReceptionProviderBusyAndCanceled(t *testing.T) {
	var calls atomic.Int32
	p := testProvider(t, "ollama", func(w http.ResponseWriter, r *http.Request) { calls.Add(1) })
	if p.client.Timeout != ProviderTimeout || ProviderTimeout != 15*time.Second {
		t.Fatal("deadline changed")
	}
	req := internbridge.Request{Text: "What happened today?", Operation: internbridge.Reception, DataClass: internbridge.Public}
	p.busy <- struct{}{}
	if _, err := p.Complete(context.Background(), req); err != internbridge.ErrUnavailable {
		t.Fatal(err)
	}
	<-p.busy
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := p.Complete(ctx, req); err != internbridge.ErrUnavailable {
		t.Fatal(err)
	}
	if calls.Load() != 0 {
		t.Fatal("busy/canceled classification reached provider")
	}
}
