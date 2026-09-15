package bridge

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"

	"go.autonomous.ai/os/system/lib/internbridge"
)

func bridgeFixture(t *testing.T, h *handler) *internbridge.Client {
	t.Helper()
	s := httptest.NewServer(h)
	t.Cleanup(s.Close)
	_, port, _ := net.SplitHostPort(s.Listener.Addr().String())
	n, _ := strconv.Atoi(port)
	c, err := internbridge.New(uint16(n))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(c.CloseIdleConnections)
	return c
}

func TestBridgeUnchangedClientContract(t *testing.T) {
	var calls atomic.Int32
	p := testProvider(t, "ollama", func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var b map[string]any
		_ = json.NewDecoder(r.Body).Decode(&b)
		output := "Hello there."
		if strings.Contains(b["messages"].([]any)[0].(map[string]any)["content"].(string), "exactly one label") {
			output = "question"
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(ollamaReply(output))
	})
	c := bridgeFixture(t, newHandler(p))
	ctx := context.Background()
	if err := c.Health(ctx); err != nil {
		t.Fatal(err)
	}
	if err := c.Ready(ctx); err != nil {
		t.Fatal(err)
	}
	if v, err := c.BridgeVersion(ctx); err != nil || v != internbridge.Version {
		t.Fatal(v, err)
	}
	if calls.Load() != 0 {
		t.Fatal("metadata triggered inference")
	}
	for _, tc := range []struct {
		input, destination string
		want               error
	}{
		{"Gus, hello", "orchestration@gus", nil}, {"hey_gus, Rex: hello", "rex@dru", nil},
		{"Rex hello", "rex@dru", nil}, {"Gus Melvil hello", "melvil@lab", nil},
		{"curator hello", "melvil@lab", nil}, {"PAM hello", "pam@gus", internbridge.ErrServiceRoute},
		{"Gus news headlines", "mcavoy@lab", internbridge.ErrServiceRoute},
		{"smart-home lights", "smart-home", internbridge.ErrCustodyHold},
		{"Gus Cassi hello", internbridge.FirstContact, internbridge.ErrCustodyHold},
		{"casi hello", internbridge.FirstContact, internbridge.ErrCustodyHold},
		{"cassandra hello", internbridge.FirstContact, internbridge.ErrCustodyHold},
	} {
		for _, op := range []internbridge.Operation{internbridge.Route, internbridge.Reception, internbridge.Generate, internbridge.Classify} {
			r := publicRequest()
			r.Text = tc.input
			r.Operation = op
			got, err := c.Do(ctx, r)
			if !errors.Is(err, tc.want) {
				t.Fatalf("%s/%s: %v", tc.input, op, err)
			}
			if tc.want != nil {
				if got != nil {
					t.Fatal("hold returned result")
				}
				continue
			}
			if got.Destination != internbridge.FirstContact || got.RequestedDestination != tc.destination || got.ReceptionRoute.Executed {
				t.Fatalf("bad reception: %+v", got)
			}
			if op == internbridge.Generate && (got.Status != "draft" || got.Output != "Hello there.") {
				t.Fatal("generation missing")
			}
			if op == internbridge.Classify && (got.Status != "classified" || got.Output != "question") {
				t.Fatal("classification missing")
			}
		}
	}
	if calls.Load() != 10 {
		t.Fatalf("expected only 5 persona generate/classify pairs; got %d", calls.Load())
	}
	if err := c.ProbeGeneration(ctx); err != nil {
		t.Fatal(err)
	}
}

func rawRequest(h http.Handler, body string) *httptest.ResponseRecorder {
	r := httptest.NewRequest("POST", "/v1/intern", strings.NewReader(body))
	r.Host = Address
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}

func TestBridgeDirectAdmissionAndReplay(t *testing.T) {
	var calls atomic.Int32
	p := testProvider(t, "ollama", func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(ollamaReply("Hello"))
	})
	h := newHandler(p)
	for _, class := range []string{"restricted", "secret", "unknown"} {
		for _, op := range []string{"route", "reception", "generate", "classify"} {
			w := rawRequest(h, fmt.Sprintf(`{"text":"private sentinel","operation":%q,"data_class":%q}`, op, class))
			if w.Code != 200 || strings.Contains(w.Body.String(), "private sentinel") {
				t.Fatal("admission leaked text")
			}
			var b map[string]any
			_ = json.Unmarshal(w.Body.Bytes(), &b)
			if class != "unknown" {
				if _, ok := b["output"]; ok {
					t.Fatal("hold includes output")
				}
				if b["status"] != "custody_hold" {
					t.Fatal("missing hold")
				}
			} else if b["status"] != "needs_classification" {
				t.Fatal("unknown admitted")
			}
		}
	}
	if calls.Load() != 0 {
		t.Fatal("held data reached provider")
	}
	c := bridgeFixture(t, h)
	r := publicRequest()
	r.RunID = "unique-1"
	if _, err := c.Do(context.Background(), r); err != nil {
		t.Fatal(err)
	}
	if _, err := c.Do(context.Background(), r); !errors.Is(err, internbridge.ErrRejected) {
		t.Fatal(err)
	}
	if calls.Load() != 1 {
		t.Fatal("replay called provider")
	}
	for i := len(h.ids); i < maxRunIDs; i++ {
		h.ids[fmt.Sprint(i)] = struct{}{}
	}
	r.RunID = "new-id"
	if _, err := c.Do(context.Background(), r); !errors.Is(err, internbridge.ErrUnavailable) {
		t.Fatal(err)
	}
}

func TestBridgeRejectsMalformedRequests(t *testing.T) {
	h := newHandler(nil)
	for _, body := range []string{
		`null`, `[]`, `{}`, `{"text":"hello","operation":"generate"}`,
		`{"text":"hello","operation":"generate","data_class":"public","data_class":"secret"}`,
		`{"text":"hello","operation":"generate","data_class":"public","\u0064ata_class":"secret"}`,
		`{"Text":"hello","operation":"generate","data_class":"public"}`,
		`{"text":"hello","operation":"generate","data_class":"public","extra":1}`,
		`{"text":"hello","operation":"generate","data_class":null}`,
		`{"text":"\ud800","operation":"generate","data_class":"public"}`,
		`{"text":"hello","operation":"generate","data_class":"public","run_id":""}`,
		`{"text":"hello","operation":"generate","data_class":"public"} {}`,
		`{"text":{},"operation":"generate","data_class":"public"}`,
		string([]byte{'{', 0xff, '}'}),
	} {
		if w := rawRequest(h, body); w.Code != 400 {
			t.Errorf("malformed accepted: %q => %d", body, w.Code)
		}
	}
	if w := rawRequest(h, strings.Repeat("a", internbridge.MaxRequestBytes+1)); w.Code != 413 {
		t.Fatal(w.Code)
	}
	w := rawRequest(h, `{"text":"\ud83d\ude00","operation":"route","data_class":"public"}`)
	if w.Code != 200 {
		t.Fatal("valid emoji rejected")
	}
	for _, header := range []string{"Origin", "Authorization", "Cookie", "Forwarded", "X-Forwarded-Host", "Content-Encoding"} {
		r := httptest.NewRequest("GET", "/health", nil)
		r.Host = Address
		r.Header.Set(header, "private sentinel")
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != 400 || strings.Contains(w.Body.String(), "private sentinel") {
			t.Fatal("unsafe ingress")
		}
	}
	for _, target := range []string{"/health?x=1", "/health?", "http://example.com/health", "/v1//intern"} {
		r := httptest.NewRequest("GET", target, nil)
		r.Host = Address
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != 400 && w.Code != 404 {
			t.Fatal("unexpected redirect/success", w.Code)
		}
	}
	r := httptest.NewRequest("GET", "/health", nil)
	r.Host = "evil.example:8765"
	w = httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != 400 {
		t.Fatal("host accepted")
	}
}

func TestBridgeUnavailableAndSafeMetadata(t *testing.T) {
	c := bridgeFixture(t, newHandler(nil))
	ctx := context.Background()
	for _, op := range []internbridge.Operation{internbridge.Generate, internbridge.Classify} {
		r := publicRequest()
		r.Operation = op
		if got, err := c.Do(ctx, r); got != nil || !errors.Is(err, internbridge.ErrUnavailable) {
			t.Fatal(got, err)
		}
	}
	r := publicRequest()
	r.Operation = internbridge.Reception
	got, err := c.Do(ctx, r)
	if err != nil || got.ReceptionRoute.Handoff != "" || got.ReceptionRoute.Intent != "unknown" {
		t.Fatal(got, err)
	}
	r.Operation = internbridge.Generate
	r.Text = "Gus"
	if _, err := c.Do(ctx, r); !errors.Is(err, internbridge.ErrNeedsInput) {
		t.Fatal(err)
	}
	// Untrusted markers are never reflected, even on model-free routes.
	raw, _ := json.Marshal(internbridge.Request{Text: "Gus <think>private [HW:move] tool_calls", Operation: internbridge.Route, DataClass: internbridge.Public})
	w := rawRequest(newHandler(nil), string(raw))
	if bytes.Contains(w.Body.Bytes(), []byte("private")) {
		t.Fatal("input reflected")
	}
}

func TestBridgeOneReceptionProposalAndOneProviderCall(t *testing.T) {
	for _, kind := range []string{"ollama", "cerebras"} {
		t.Run(kind, func(t *testing.T) {
			var calls atomic.Int32
			p := testProvider(t, kind, func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				var b struct {
					Messages []struct{ Content string } `json:"messages"`
				}
				_ = json.NewDecoder(r.Body).Decode(&b)
				if len(b.Messages) != 2 || b.Messages[1].Content != "Hey Gus, Rex, ask Melvil and PAM for a greeting." {
					t.Error("admitted text changed or extra context attached")
				}
				w.Header().Set("Content-Type", "application/json")
				if kind == "ollama" {
					_ = json.NewEncoder(w).Encode(ollamaReply("Hello there."))
				} else {
					fmt.Fprint(w, `{"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"Hello there."}}]}`)
				}
			})
			c := bridgeFixture(t, newHandler(p))
			r := publicRequest()
			r.Text = "Hey Gus, Rex, ask Melvil and PAM for a greeting."
			got, err := c.Do(context.Background(), r)
			if err != nil {
				t.Fatal(err)
			}
			if got.Destination != internbridge.FirstContact || got.RequestedDestination != "rex@dru" ||
				got.ReceptionRoute.FirstDestination != internbridge.FirstContact || got.ReceptionRoute.Handoff != "rex@dru" ||
				got.ReceptionRoute.Executed || got.ReceptionRoute.NextStep != "safe_escalation" || calls.Load() != 1 {
				t.Fatalf("multiple destinations or execution claimed: %+v, calls=%d", got, calls.Load())
			}
		})
	}
}

func TestBridgeRejectsProviderOutputWithoutReflectingIt(t *testing.T) {
	for _, output := range []string{"<think>private</think>Hello", "[Lights off](hw:/led/off)", "Analysis: private\nHello"} {
		p := testProvider(t, "ollama", func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(ollamaReply(output))
		})
		h := newHandler(p)
		w := rawRequest(h, `{"text":"Gus hello","operation":"generate","data_class":"public"}`)
		var got response
		if json.Unmarshal(w.Body.Bytes(), &got) != nil || got.Status != "fallback" || got.Output == nil || !safeOutput(*got.Output) ||
			strings.Contains(w.Body.String(), "private") || strings.Contains(w.Body.String(), "led/off") {
			t.Fatalf("unsafe fallback: %s", w.Body.String())
		}
		c := bridgeFixture(t, h)
		if result, err := c.Do(context.Background(), publicRequest()); result != nil || !errors.Is(err, internbridge.ErrUnavailable) {
			t.Fatalf("unsafe provider result reached consumer: %+v %v", result, err)
		}
	}
}
