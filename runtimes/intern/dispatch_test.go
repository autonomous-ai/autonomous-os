package intern

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"go.autonomous.ai/os/system/lib/internbridge"
)

type dispatchTransport func(*http.Request) (*http.Response, error)

func (f dispatchTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

const syntheticDispatchToken = "synthetic-intern-test-only"

func dispatchFixture(t *testing.T, f dispatchTransport) *substrateDispatcher {
	t.Helper()
	d, err := NewServiceDispatcher(DispatchConfig{Endpoint: CanonicalAuthorityURL, Principal: "intern@gus", TokenSource: func(context.Context) (string, error) { return syntheticDispatchToken, nil }})
	if err != nil {
		t.Fatal(err)
	}
	s := d.(*substrateDispatcher)
	s.http.Transport = f
	return s
}

func serviceProposal(destination string) (internbridge.Request, internbridge.Result) {
	r := internbridge.Request{RunID: "intern-test-1", Text: "public briefing", Operation: internbridge.Reception, DataClass: internbridge.Public}
	h := sha256.Sum256([]byte(r.RunID))
	intent := "briefing"
	if destination == "pam@gus" {
		intent = "reminder"
		r.Text = "remind about business report"
	}
	return r, internbridge.Result{RunID: "run-" + hex.EncodeToString(h[:12]), Destination: internbridge.FirstContact, RequestedDestination: destination,
		Kind: "service", Status: "service_route", Output: "Pending service review", ReceptionRoute: internbridge.ReceptionRoute{Handoff: destination, Intent: intent}}
}

func enqueueAck(id, destination, status string, idempotent bool) string {
	h := sha256.Sum256([]byte("bus-message\x00intern@gus\x00" + id))
	return fmt.Sprintf(`{"ok":true,"contract_version":"gus.comms/v1","message_id":%q,"idempotent":%t,"task_id":%q,"destination":%q,"status":%q}`, hex.EncodeToString(h[:8]), idempotent, id, destination, status)
}

func authorityResponse(code int, body string) *http.Response {
	return &http.Response{StatusCode: code, Header: http.Header{"Content-Type": []string{"application/json"}}, Body: io.NopCloser(strings.NewReader(body))}
}

func TestDispatchCredentialsFailClosed(t *testing.T) {
	t.Setenv("GUS_INTERN_DISPATCH_TOKEN", "")
	if s := New(); s.dispatcher != nil {
		t.Fatal("default dispatcher present")
	}
	if d, err := NewServiceDispatcher(DispatchConfig{}); err != nil || d != nil {
		t.Fatal("missing source enabled dispatch")
	}
	for _, source := range []func(context.Context) (string, error){
		func(context.Context) (string, error) { return "", nil },
		func(context.Context) (string, error) { return "", errors.New(syntheticDispatchToken) },
	} {
		d := dispatchFixture(t, func(*http.Request) (*http.Response, error) {
			t.Fatal("network used without credential")
			return nil, nil
		})
		d.tokenSource = source
		r, p := serviceProposal("mcavoy@lab")
		got, err := d.Dispatch(context.Background(), r, p)
		if got != nil || !errors.Is(err, internbridge.ErrServiceRoute) || strings.Contains(err.Error(), syntheticDispatchToken) {
			t.Fatal("unsafe missing credential result")
		}
	}
}

func TestDispatchCanonicalEndpoint(t *testing.T) {
	for _, endpoint := range []string{"http://localhost:7370", CanonicalAuthorityURL + "/", CanonicalAuthorityURL + "/dispatch", CanonicalAuthorityURL + "?x=y", "http://user:password@100.115.27.81:7370", "https://100.115.27.81:7370", CanonicalAuthorityURL + "#fragment"} {
		_, err := NewServiceDispatcher(DispatchConfig{Endpoint: endpoint, Principal: "intern@gus", TokenSource: func(context.Context) (string, error) { t.Fatal("resolved during construction"); return "", nil }})
		if !errors.Is(err, ErrDispatchConfig) {
			t.Fatal("noncanonical endpoint accepted")
		}
	}
}

func TestDispatchPrincipal(t *testing.T) {
	for _, principal := range []string{"", "fleet-dispatch@dru", "fleet-dispatch@gus", "fleet-dispatch@lab", "intern@lab", "pam@gus"} {
		if _, err := NewServiceDispatcher(DispatchConfig{Principal: principal, TokenSource: func(context.Context) (string, error) { return syntheticDispatchToken, nil }}); !errors.Is(err, ErrDispatchConfig) {
			t.Fatalf("principal %q accepted", principal)
		}
	}
}

func TestDispatchInvalidIDAndEmbeddedToken(t *testing.T) {
	for _, id := range []string{".leading", "-leading", "_leading", ""} {
		r, p := serviceProposal("mcavoy@lab")
		r.RunID = id
		h := sha256.Sum256([]byte(id))
		p.RunID = "run-" + hex.EncodeToString(h[:12])
		d := dispatchFixture(t, func(*http.Request) (*http.Response, error) { t.Fatal("invalid ID sent"); return nil, nil })
		if got, err := d.Dispatch(context.Background(), r, p); got != nil || !errors.Is(err, internbridge.ErrProtocol) {
			t.Fatal("invalid ID accepted")
		}
	}
	// A valid bearer value must never be sent as task content.
	r, p := serviceProposal("mcavoy@lab")
	r.Text = "briefing synthetic+token"
	d := dispatchFixture(t, func(*http.Request) (*http.Response, error) { t.Fatal("token sent as content"); return nil, nil })
	d.tokenSource = func(context.Context) (string, error) { return "synthetic+token", nil }
	if got, err := d.Dispatch(context.Background(), r, p); got != nil || !errors.Is(err, internbridge.ErrCustodyHold) {
		t.Fatal("embedded token accepted")
	}
}

func TestDispatchServiceProfile(t *testing.T) {
	for _, destination := range []string{"mcavoy@lab", "pam@gus"} {
		for _, intent := range []string{"news", "briefing", "notification", "alarm", "reminder", "service", "engineering", ""} {
			t.Run(destination+"/"+intent, func(t *testing.T) {
				r, p := serviceProposal(destination)
				p.ReceptionRoute.Intent = intent
				valid := (destination == "mcavoy@lab" && (intent == "news" || intent == "briefing")) ||
					(destination == "pam@gus" && (intent == "notification" || intent == "alarm" || intent == "reminder"))
				calls := 0
				d := dispatchFixture(t, func(*http.Request) (*http.Response, error) {
					calls++
					return authorityResponse(202, enqueueAck(r.RunID, destination, "queued", true)), nil
				})
				got, err := d.Dispatch(context.Background(), r, p)
				if valid {
					if err != nil || calls != 1 || got == nil || !got.Idempotent || got.Delivered {
						t.Fatal("valid idempotent enqueue rejected")
					}
				} else if !errors.Is(err, internbridge.ErrProtocol) || got != nil || calls != 0 {
					t.Fatal("invalid intent reached authority")
				}
			})
		}
	}
}

func TestDispatchTextBounds(t *testing.T) {
	for _, tc := range []struct {
		text  string
		valid bool
	}{
		{strings.Repeat("a", 2000), true}, {strings.Repeat("é", 1000), true},
		{strings.Repeat("a", 2001), false}, {strings.Repeat("é", 1001), false},
		{" briefing", false}, {"briefing ", false}, {"briefing\nnow", false},
		{"briefing\t", false}, {"briefing\x00", false}, {"briefing\u200b", false},
		{"briefing\xff", false}, {"", false},
	} {
		r, p := serviceProposal("mcavoy@lab")
		r.Text = tc.text
		calls := 0
		d := dispatchFixture(t, func(*http.Request) (*http.Response, error) {
			calls++
			return authorityResponse(200, enqueueAck(r.RunID, p.RequestedDestination, "accepted", false)), nil
		})
		got, err := d.Dispatch(context.Background(), r, p)
		if tc.valid && (err != nil || got == nil || calls != 1) || !tc.valid && (err == nil || got != nil || calls != 0) {
			t.Fatalf("unexpected admission for %d bytes", len(tc.text))
		}
	}
}

func TestDispatchAckContract(t *testing.T) {
	valid := enqueueAck("intern-test-1", "mcavoy@lab", "queued", false)
	for _, key := range []string{"ok", "contract_version", "message_id", "idempotent", "task_id", "destination", "status"} {
		for _, value := range []any{nil, 17, "wrong", true, map[string]any{}} {
			// true is valid for these boolean fields.
			if value == true && (key == "ok" || key == "idempotent") {
				continue
			}
			t.Run(key+fmt.Sprintf("/%T/%v", value, value), func(t *testing.T) {
				var ack map[string]any
				_ = json.Unmarshal([]byte(valid), &ack)
				ack[key] = value
				raw, _ := json.Marshal(ack)
				assertRejectedAck(t, string(raw))
				delete(ack, key)
				raw, _ = json.Marshal(ack)
				assertRejectedAck(t, string(raw))
			})
		}
	}
	for _, suffix := range []string{`,"delivered":true}`, `,"delivered":null}`, `,"body":{"extra":true}}`, `,"content":"` + syntheticDispatchToken + `"}`, `,"ok":true}`} {
		assertRejectedAck(t, strings.TrimSuffix(valid, "}")+suffix)
	}
	for _, status := range []string{"REPORTED_COMPLETE", "delivered", "completed"} {
		assertRejectedAck(t, strings.Replace(valid, `"queued"`, fmt.Sprintf("%q", status), 1))
	}
	assertRejectedAck(t, valid+" {}")
	assertRejectedAck(t, valid+" garbage")
}

func assertRejectedAck(t *testing.T, body string) {
	t.Helper()
	d := dispatchFixture(t, func(*http.Request) (*http.Response, error) { return authorityResponse(200, body), nil })
	r, p := serviceProposal("mcavoy@lab")
	if got, err := d.Dispatch(context.Background(), r, p); got != nil || !errors.Is(err, ErrDispatch) || strings.Contains(err.Error(), syntheticDispatchToken) {
		t.Fatal("unsafe acknowledgement accepted")
	}
}

func TestDispatchInFlightCancellationAndLimits(t *testing.T) {
	r, p := serviceProposal("mcavoy@lab")
	ctx, cancel := context.WithCancel(context.Background())
	d := dispatchFixture(t, func(req *http.Request) (*http.Response, error) {
		deadline, ok := req.Context().Deadline()
		if !ok || time.Until(deadline) > internbridge.RequestTimeout {
			t.Fatal("missing timeout")
		}
		cancel()
		<-req.Context().Done()
		return nil, req.Context().Err()
	})
	if got, err := d.Dispatch(ctx, r, p); got != nil || !errors.Is(err, ErrDispatch) {
		t.Fatal("unsafe canceled outcome")
	}
	for _, mutate := range []func(*http.Response){
		func(resp *http.Response) { resp.Header.Set("Content-Type", "text/plain") },
		func(resp *http.Response) { resp.Header.Set("Content-Encoding", "gzip") },
		func(resp *http.Response) { resp.StatusCode = 401 },
		func(resp *http.Response) { resp.StatusCode = 403 },
	} {
		d := dispatchFixture(t, func(*http.Request) (*http.Response, error) {
			resp := authorityResponse(200, enqueueAck(r.RunID, p.RequestedDestination, "queued", false))
			mutate(resp)
			return resp, nil
		})
		if got, err := d.Dispatch(context.Background(), r, p); got != nil || !errors.Is(err, ErrDispatch) {
			t.Fatal("invalid response accepted")
		}
	}
}

func TestDispatchPayloadHeadersAndReceipt(t *testing.T) {
	for _, destination := range []string{"mcavoy@lab", "pam@gus"} {
		t.Run(destination, func(t *testing.T) {
			r, p := serviceProposal(destination)
			if destination == "pam@gus" {
				r.Text = "remind about business report"
				r.DataClass = internbridge.Business
			}
			calls := 0
			d := dispatchFixture(t, func(req *http.Request) (*http.Response, error) {
				calls++
				if req.Method != "POST" || req.URL.String() != CanonicalAuthorityURL+"/messages/post-task" || req.Header.Get("Authorization") != "Bearer "+syntheticDispatchToken || req.Header.Get("X-GUS-Principal") != "intern@gus" || req.Header.Get("Content-Type") != "application/json" {
					t.Error("incorrect request contract")
				}
				var body map[string]any
				if json.NewDecoder(req.Body).Decode(&body) != nil {
					t.Fatal("bad payload")
				}
				if len(body) != 6 || body["request_id"] != r.RunID || body["role"] != "intern@gus" || body["node"] != "gus" || body["to_role"] != destination {
					t.Error("incorrect payload fields")
				}
				stamp := body["requested_at"].(float64)
				if stamp != float64(int64(stamp)) || time.Now().Unix()-int64(stamp) > 2 {
					t.Error("incorrect request timestamp")
				}
				task := body["task"].(map[string]any)
				if len(task) != 6 || task["schema_version"] != "gus-bus-task/v1" || task["task_id"] != r.RunID || task["data_zone"] != string(r.DataClass) || task["custody_policy"] != "business-public-only" || task["instruction_inert"] != true {
					t.Error("incorrect task contract")
				}
				content := task["body"].(map[string]any)
				if len(content) != 4 || content["destination"] != destination || content["service_intent"] != p.ReceptionRoute.Intent || content["request_text"] != r.Text || content["idempotency_key"] != body["request_id"] {
					t.Error("incorrect service body")
				}
				return authorityResponse(200, enqueueAck(r.RunID, destination, "accepted", false)), nil
			})
			got, err := d.Dispatch(context.Background(), r, p)
			if err != nil || got == nil {
				t.Fatal("valid authority result rejected")
			}
			if calls != 1 || got.RunID != r.RunID || got.TaskID != r.RunID || got.BridgeRunID != p.RunID || got.Delivered || got.Status != "accepted" || got.MessageID == "" || got.Idempotent {
				t.Fatal("incorrect receipt")
			}
			raw, _ := json.Marshal(got)
			if strings.Contains(string(raw), syntheticDispatchToken) {
				t.Fatal("credential leaked")
			}
		})
	}
}

func TestDispatchCustodyAndCorrelation(t *testing.T) {
	for _, dest := range []string{"smart-home", "unknown", "rex@dru", "cassi@mama"} {
		d := dispatchFixture(t, func(*http.Request) (*http.Response, error) { t.Fatal("held route reached network"); return nil, nil })
		r, p := serviceProposal(dest)
		if _, err := d.Dispatch(context.Background(), r, p); !errors.Is(err, internbridge.ErrCustodyHold) {
			t.Fatal("destination not held")
		}
	}
	for _, mutate := range []func(*internbridge.Request, *internbridge.Result){
		func(r *internbridge.Request, p *internbridge.Result) { r.Text = "turn on lights" },
		func(r *internbridge.Request, p *internbridge.Result) { r.DataClass = internbridge.Unknown },
		func(r *internbridge.Request, p *internbridge.Result) { r.DataClass = internbridge.Secret },
		func(r *internbridge.Request, p *internbridge.Result) { p.RunID = "run-wrong" },
		func(r *internbridge.Request, p *internbridge.Result) { p.ReceptionRoute.Executed = true },
		func(r *internbridge.Request, p *internbridge.Result) { r.Text = syntheticDispatchToken },
	} {
		d := dispatchFixture(t, func(*http.Request) (*http.Response, error) {
			t.Fatal("invalid request reached network")
			return nil, nil
		})
		r, p := serviceProposal("mcavoy@lab")
		mutate(&r, &p)
		if got, err := d.Dispatch(context.Background(), r, p); err == nil || got != nil {
			t.Fatal("invalid request accepted")
		}
	}
}

func TestDispatchMalformedFailureAndSecretLeakage(t *testing.T) {
	for _, status := range []string{"completed", "complete", "delivered", "REPORTED_COMPLETE", "success", "failed"} {
		d := dispatchFixture(t, func(*http.Request) (*http.Response, error) {
			return authorityResponse(200, fmt.Sprintf(`{"ok":true,"status":%q,"task_id":"intern-test-1","worker":"inference@lab"}`, status)), nil
		})
		r, p := serviceProposal("mcavoy@lab")
		if got, err := d.Dispatch(context.Background(), r, p); got != nil || !errors.Is(err, ErrDispatch) {
			t.Fatal("non-acknowledgement status accepted")
		}
	}
	for _, body := range []string{`{"ok":true,"status":"accepted","task_id":"intern-test-1","delivered":true}`, `{"ok":true,"status":"accepted","task_id":"intern-test-1","delivered":null}`, "{\"invalid\":\"\xff\"}"} {
		d := dispatchFixture(t, func(*http.Request) (*http.Response, error) { return authorityResponse(200, body), nil })
		r, p := serviceProposal("mcavoy@lab")
		if got, err := d.Dispatch(context.Background(), r, p); got != nil || !errors.Is(err, ErrDispatch) {
			t.Fatal("contradictory or malformed response accepted")
		}
	}
	for _, body := range []string{`{`, `null`, `{}`, `{"ok":false,"status":"queued","task_id":"intern-test-1"}`, `{"ok":true,"status":"delivered","task_id":"intern-test-1"}`, `{"ok":true,"status":"queued","task_id":"wrong"}`, `{"ok":true,"ok":false,"status":"queued","task_id":"intern-test-1"}`, `{"ok":true,"status":"queued","task_id":"intern-test-1"} {}`, `{"ok":true,"status":"REPORTED_COMPLETE","task_id":"intern-test-1","worker":"inference@mama"}`, strings.Repeat("x", internbridge.MaxResponseBytes+1)} {
		d := dispatchFixture(t, func(*http.Request) (*http.Response, error) { return authorityResponse(200, body), nil })
		r, p := serviceProposal("mcavoy@lab")
		if got, err := d.Dispatch(context.Background(), r, p); got != nil || !errors.Is(err, ErrDispatch) {
			t.Fatal("invalid response accepted")
		}
	}
	for _, code := range []int{301, 401, 403, 500, 503} {
		d := dispatchFixture(t, func(*http.Request) (*http.Response, error) {
			return authorityResponse(code, syntheticDispatchToken), nil
		})
		r, p := serviceProposal("mcavoy@lab")
		got, err := d.Dispatch(context.Background(), r, p)
		if got != nil || !errors.Is(err, ErrDispatch) || strings.Contains(err.Error(), syntheticDispatchToken) {
			t.Fatal("unsafe failed response")
		}
	}
	d := dispatchFixture(t, func(*http.Request) (*http.Response, error) { return nil, errors.New(syntheticDispatchToken) })
	r, p := serviceProposal("mcavoy@lab")
	_, err := d.Dispatch(context.Background(), r, p)
	if err == nil || strings.Contains(err.Error(), syntheticDispatchToken) {
		t.Fatal("unsafe transport error")
	}
}

func TestDispatchRuntimeConfigurationAndCancellation(t *testing.T) {
	t.Setenv("GUS_INTERN_DISPATCH_TOKEN", syntheticDispatchToken)
	t.Setenv("GUS_INTERN_DISPATCH_PRINCIPAL", "unassigned")
	if New().dispatcher != nil {
		t.Fatal("invalid principal enabled default dispatcher")
	}
	t.Setenv("GUS_INTERN_DISPATCH_PRINCIPAL", "intern@gus")
	t.Setenv("GUS_INTERN_DISPATCH_URL", "http://127.0.0.1:7370")
	if New().dispatcher != nil {
		t.Fatal("invalid URL enabled default dispatcher")
	}
	t.Setenv("GUS_INTERN_DISPATCH_URL", CanonicalAuthorityURL)
	s := New()
	if s.dispatcher == nil {
		t.Fatal("explicit environment configuration not wired")
	}
	d := s.dispatcher.(*substrateDispatcher)
	d.http.Transport = dispatchTransport(func(*http.Request) (*http.Response, error) {
		t.Fatal("network after credential removal or cancellation")
		return nil, nil
	})
	r, p := serviceProposal("pam@gus")
	t.Setenv("GUS_INTERN_DISPATCH_TOKEN", "")
	if _, err := d.Dispatch(context.Background(), r, p); !errors.Is(err, internbridge.ErrServiceRoute) {
		t.Fatal("credential removal not fail closed")
	}
	t.Setenv("GUS_INTERN_DISPATCH_TOKEN", syntheticDispatchToken)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := d.Dispatch(ctx, r, p); !errors.Is(err, internbridge.ErrCanceled) {
		t.Fatal("canceled dispatch accepted")
	}
}

func TestDispatchRedirectNoForward(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1) }))
	defer server.Close()
	d := dispatchFixture(t, func(*http.Request) (*http.Response, error) {
		resp := authorityResponse(302, "")
		resp.Header.Set("Location", server.URL)
		return resp, nil
	})
	r, p := serviceProposal("pam@gus")
	if _, err := d.Dispatch(context.Background(), r, p); err == nil {
		t.Fatal("redirect accepted")
	}
	if calls.Load() != 0 {
		t.Fatal("redirect forwarded")
	}
}

func TestServiceDispatchIntegration(t *testing.T) {
	for _, configured := range []bool{false, true} {
		for _, destination := range []string{"mcavoy@lab", "pam@gus", "rex@dru"} {
			t.Run(fmt.Sprintf("%t/%s", configured, destination), func(t *testing.T) {
				s := bridgeFixture(t, func(w http.ResponseWriter, r *http.Request) {
					var req internbridge.Request
					_ = json.NewDecoder(r.Body).Decode(&req)
					body := envelope(req)
					if destination != "rex@dru" {
						body["requested_destination"] = destination
						body["kind"] = "service"
						body["status"] = "service_route"
						body["reception_route"].(map[string]any)["handoff"] = destination
						intent := "briefing"
						if destination == "pam@gus" {
							intent = "reminder"
						}
						body["reception_route"].(map[string]any)["intent"] = intent
					}
					w.Header().Set("Content-Type", "application/json")
					_ = json.NewEncoder(w).Encode(body)
				})
				var calls atomic.Int32
				if configured {
					s.dispatcher = dispatchFixture(t, func(r *http.Request) (*http.Response, error) {
						calls.Add(1)
						var body map[string]any
						_ = json.NewDecoder(r.Body).Decode(&body)
						return authorityResponse(202, enqueueAck(body["request_id"].(string), destination, "queued", false)), nil
					})
				}
				startWorker(t, s)
				id, err := s.Submit(admitted(t, "public briefing"))
				if err != nil {
					t.Fatal(err)
				}
				var turn Turn
				until(t, func() bool {
					turn, _ = s.Result(id)
					return turn.Dispatch != nil || turn.State == "failed" || turn.State == "completed"
				})
				if destination == "rex@dru" {
					if calls.Load() != 0 || turn.State != "completed" || turn.Dispatch != nil {
						t.Fatal("persona dispatched")
					}
					return
				}
				if !configured {
					if turn.Error != internbridge.ErrServiceRoute.Error() || turn.Result != nil {
						t.Fatal("missing dispatcher not fail closed")
					}
					return
				}
				if calls.Load() != 1 || turn.State != "queued" || turn.Scope != "service_dispatch" || turn.Dispatch.RunID != id || turn.Dispatch.Delivered || turn.ExecutesActions {
					t.Fatal("invalid service receipt")
				}
				turn.Dispatch.Delivered = true
				again, _ := s.Result(id)
				if again.Dispatch.Delivered {
					t.Fatal("receipt aliases stored result")
				}
			})
		}
	}
}
