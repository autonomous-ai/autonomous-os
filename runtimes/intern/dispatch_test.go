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

	"go.autonomous.ai/os/system/lib/internbridge"
)

type dispatchTransport func(*http.Request) (*http.Response, error)

func (f dispatchTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

const syntheticDispatchToken = "synthetic-intern-test-only"

func dispatchFixture(t *testing.T, f dispatchTransport) *substrateDispatcher {
	t.Helper()
	d, err := NewServiceDispatcher(DispatchConfig{Endpoint: CanonicalAuthorityURL, Principal: "fleet-dispatch@dru", TokenSource: func(context.Context) (string, error) { return syntheticDispatchToken, nil }})
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
	return r, internbridge.Result{RunID: "run-" + hex.EncodeToString(h[:12]), Destination: internbridge.FirstContact, RequestedDestination: destination,
		Kind: "service", Status: "service_route", Output: "Pending service review", ReceptionRoute: internbridge.ReceptionRoute{Handoff: destination}}
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
		_, err := NewServiceDispatcher(DispatchConfig{Endpoint: endpoint, Principal: "fleet-dispatch@dru", TokenSource: func(context.Context) (string, error) { t.Fatal("resolved during construction"); return "", nil }})
		if !errors.Is(err, ErrDispatchConfig) {
			t.Fatal("noncanonical endpoint accepted")
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
				if req.Method != "POST" || req.URL.String() != CanonicalAuthorityURL+"/dispatch" || req.Header.Get("Authorization") != "Bearer "+syntheticDispatchToken || req.Header.Get("X-GUS-Principal") != "fleet-dispatch@dru" || req.Header.Get("Content-Type") != "application/json" {
					t.Error("incorrect request contract")
				}
				var body map[string]any
				if json.NewDecoder(req.Body).Decode(&body) != nil {
					t.Fatal("bad payload")
				}
				class := "PUBLIC"
				if destination == "pam@gus" {
					class = "INTERNAL"
				}
				if len(body) != 8 || body["task_id"] != r.RunID || body["request_id"] != r.RunID || body["task"] != r.Text || body["classification"] != class || body["dry_run"] != false || body["source"] != "intern" {
					t.Error("incorrect payload fields")
				}
				requirements := body["requirements"].(map[string]any)
				parts := strings.Split(destination, "@")
				if requirements["node"] != parts[1] || requirements["roles"].([]any)[0] != parts[0] {
					t.Error("incorrect authority routing constraint")
				}
				return authorityResponse(200, fmt.Sprintf(`{"ok":true,"status":"accepted","task_id":%q,"content":%q}`, r.RunID, syntheticDispatchToken)), nil
			})
			got, err := d.Dispatch(context.Background(), r, p)
			if err != nil || got == nil {
				t.Fatal("valid authority result rejected")
			}
			if calls != 1 || got.RunID != r.RunID || got.TaskID != r.RunID || got.BridgeRunID != p.RunID || got.Delivered || got.Status != "accepted" {
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
	t.Setenv("GUS_INTERN_DISPATCH_PRINCIPAL", "fleet-dispatch@dru")
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
						return authorityResponse(202, fmt.Sprintf(`{"ok":true,"status":"queued","task_id":%q}`, body["task_id"])), nil
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
