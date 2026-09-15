package internbridge

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func fixture(t *testing.T, handler http.HandlerFunc) *Client {
	t.Helper()
	s := httptest.NewServer(handler)
	t.Cleanup(s.Close)
	c, err := New(uint16(s.Listener.Addr().(*net.TCPAddr).Port))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(c.CloseIdleConnections)
	return c
}
func reply(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}
func result(status string) map[string]any {
	return map[string]any{"version": Version, "run_id": "run-0123456789abcdef0123456789abcdef", "destination": "orchestration@gus", "kind": "persona", "status": status, "executes_actions": false, "transport_status": "accepted", "lifecycle_status": "completed", "output": "safe draft"}
}
func request() Request { return Request{Text: "hello", Operation: Generate, DataClass: Business} }
func assertError(t *testing.T, err, want error) {
	t.Helper()
	if !errors.Is(err, want) {
		t.Fatalf("error = %v, want %v", err, want)
	}
	var safe *Error
	if !errors.As(err, &safe) {
		t.Fatalf("not a typed error: %T", err)
	}
}

func TestSuccessAndCorrelation(t *testing.T) {
	for _, op := range []Operation{Route, Classify, Generate} {
		t.Run(string(op), func(t *testing.T) {
			r := request()
			r.Operation = op
			r.RunID = "opaque-1"
			c := fixture(t, func(w http.ResponseWriter, req *http.Request) {
				if req.Method != "POST" || req.URL.Path != "/v1/intern" || req.Header.Get("Authorization") != "" || req.Header.Get("Content-Type") != "application/json" {
					t.Error("incorrect wire request")
				}
				var got Request
				if err := json.NewDecoder(req.Body).Decode(&got); err != nil || got != r {
					t.Error("request not preserved")
				}
				status := map[Operation]string{Route: "routed", Classify: "classified", Generate: "draft"}[op]
				body := result(status)
				hash := sha256.Sum256([]byte(r.RunID))
				body["run_id"] = "run-" + hex.EncodeToString(hash[:12])
				if op == Classify {
					body["output"] = "question"
				}
				reply(w, 200, body)
			})
			got, err := c.Do(context.Background(), r)
			if err != nil || got == nil || got.Output == "" {
				t.Fatalf("result=%v error=%v", got, err)
			}
		})
	}
}

func TestProbes(t *testing.T) {
	c := fixture(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" {
			t.Error("probe must GET")
		}
		status := "ok"
		if r.URL.Path == "/ready" {
			status = "ready"
		}
		reply(w, 200, map[string]any{"version": Version, "status": status, "transport": "loopback", "executes_actions": false})
	})
	if err := c.Health(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := c.Ready(context.Background()); err != nil {
		t.Fatal(err)
	}
	if v, err := c.BridgeVersion(context.Background()); err != nil || v != Version {
		t.Fatalf("version=%q error=%v", v, err)
	}
}

func TestInvalidRequestNeverSent(t *testing.T) {
	var calls atomic.Int32
	c := fixture(t, func(w http.ResponseWriter, r *http.Request) { calls.Add(1) })
	cases := []Request{
		{}, {Text: "hello", Operation: Generate}, {Text: "hello", DataClass: Public},
		{Text: "hello", Operation: "execute", DataClass: Public}, {Text: "hello", Operation: Route, DataClass: "trusted"},
		{Text: " \n\t", Operation: Route, DataClass: Unknown}, {Text: string([]byte{0xff}), Operation: Route, DataClass: Unknown},
		{Text: strings.Repeat("a", 8001), Operation: Route, DataClass: Unknown},
		{Text: strings.Repeat("界", 8000), Operation: Route, DataClass: Unknown},
		{Text: strings.Repeat("<", 8000), Operation: Route, DataClass: Unknown},
		{Text: "hello", Operation: Route, DataClass: Unknown, RunID: strings.Repeat("a", 65)},
		{Text: "hello", Operation: Route, DataClass: Unknown, RunID: "unsafe/id"},
	}
	for i, r := range cases {
		_, err := c.Do(context.Background(), r)
		if !errors.Is(err, ErrInvalidRequest) {
			t.Errorf("case %d: %v", i, err)
		}
	}
	if calls.Load() != 0 {
		t.Fatal("invalid request reached server")
	}
	if _, err := New(0); !errors.Is(err, ErrInvalidRequest) {
		t.Fatal("zero port accepted")
	}
	_, err := c.Do(nil, request())
	assertError(t, err, ErrInvalidRequest)
}

func TestApplicationStatuses(t *testing.T) {
	for _, tc := range []struct {
		status string
		class  DataClass
		dest   string
		want   error
	}{
		{"custody_hold", Restricted, "cassi@mama", ErrCustodyHold},
		{"custody_hold", Secret, "orchestration@gus", ErrCustodyHold},
		{"needs_classification", Unknown, "orchestration@gus", ErrNeedsClassification},
		{"needs_input", Public, "orchestration@gus", ErrNeedsInput},
		{"fallback", Business, "orchestration@gus", ErrUnavailable},
		{"service_route", Business, "pam@gus", ErrServiceRoute},
	} {
		t.Run(tc.status+string(tc.class), func(t *testing.T) {
			c := fixture(t, func(w http.ResponseWriter, r *http.Request) {
				body := result(tc.status)
				body["destination"] = tc.dest
				if tc.dest == "pam@gus" {
					body["kind"] = "service"
				}
				if tc.status == "custody_hold" {
					delete(body, "output")
				}
				reply(w, 200, body)
			})
			r := request()
			r.DataClass = tc.class
			got, err := c.Do(context.Background(), r)
			assertError(t, err, tc.want)
			if got != nil {
				t.Fatal("non-success returned content")
			}
		})
	}
}

func TestStrictResponse(t *testing.T) {
	for _, tc := range []struct {
		name   string
		mutate func(map[string]any)
	}{
		{"version", func(b map[string]any) { b["version"] = "0.2.0" }},
		{"unknown", func(b map[string]any) { b["extra"] = "x" }},
		{"missing", func(b map[string]any) { delete(b, "executes_actions") }},
		{"null", func(b map[string]any) { b["executes_actions"] = nil }},
		{"action", func(b map[string]any) { b["executes_actions"] = true }},
		{"wrong type", func(b map[string]any) { b["output"] = 42 }},
		{"nested", func(b map[string]any) { b["output"] = map[string]string{"x": "y"} }},
		{"status", func(b map[string]any) { b["status"] = "success" }},
		{"operation mismatch", func(b map[string]any) { b["status"] = "classified"; b["output"] = "question" }},
		{"transport", func(b map[string]any) { b["transport_status"] = "rejected" }},
		{"lifecycle", func(b map[string]any) { b["lifecycle_status"] = "running" }},
		{"hold leak", func(b map[string]any) { b["status"] = "custody_hold" }},
		{"run id", func(b map[string]any) { b["run_id"] = "echoed-message" }},
		{"destination", func(b map[string]any) { b["destination"] = "other" }},
		{"kind", func(b map[string]any) { b["kind"] = "service" }},
		{"hardware", func(b map[string]any) { b["output"] = "[HW:move]" }},
		{"reasoning", func(b map[string]any) { b["output"] = "<think>secret" }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c := fixture(t, func(w http.ResponseWriter, r *http.Request) { b := result("draft"); tc.mutate(b); reply(w, 200, b) })
			_, err := c.Do(context.Background(), request())
			assertError(t, err, ErrProtocol)
		})
	}
	for _, raw := range []string{`null`, `[]`, `{`, `{"version":"0.1.0","version":"0.1.0"}`, `{} {}`, string([]byte{0xff})} {
		c := fixture(t, func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, raw)
		})
		_, err := c.Do(context.Background(), request())
		assertError(t, err, ErrProtocol)
	}
}

func TestSafeHTTPFailures(t *testing.T) {
	const sensitive = "NEVER-ECHO-private-body"
	for _, status := range []int{400, 404, 409, 413, 500} {
		t.Run(fmt.Sprint(status), func(t *testing.T) {
			c := fixture(t, func(w http.ResponseWriter, r *http.Request) {
				b := map[string]any{"version": Version, "error": sensitive}
				if status != 404 {
					b["executes_actions"] = false
					b["transport_status"] = "rejected"
					b["lifecycle_status"] = "not_started"
				}
				if status == 400 || status == 409 || status == 500 {
					b["run_id"] = "run-rejected"
					b["destination"] = nil
					b["kind"] = nil
					b["status"] = "rejected"
				}
				if status == 500 {
					b["transport_status"] = "failed"
					b["lifecycle_status"] = "failed"
				}
				reply(w, status, b)
			})
			got, err := c.Do(context.Background(), request())
			want := ErrRejected
			if status == 500 {
				want = ErrUnavailable
			}
			assertError(t, err, want)
			if got != nil || strings.Contains(fmt.Sprintf("%+v", err), sensitive) {
				t.Fatal("error leaked content")
			}
			var safe *Error
			errors.As(err, &safe)
			if safe.HTTPStatus != status {
				t.Fatal("status missing")
			}
		})
	}
}

func TestLimitsAndDeadlines(t *testing.T) {
	c := fixture(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, strings.Repeat("x", MaxResponseBytes+1))
	})
	_, err := c.Do(context.Background(), request())
	assertError(t, err, ErrResponseTooLarge)
	var calls atomic.Int32
	c = fixture(t, func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		select {
		case <-r.Context().Done():
		case <-time.After(150 * time.Millisecond):
		}
	})
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	_, err = c.Do(ctx, request())
	assertError(t, err, ErrDeadline)
	if calls.Load() != 1 {
		t.Fatal("request retried")
	}
	ctx, cancel = context.WithCancel(context.Background())
	cancel()
	_, err = c.Do(ctx, request())
	assertError(t, err, ErrCanceled)
	if calls.Load() != 1 {
		t.Fatal("canceled request sent")
	}
}

func TestNoProxyOrRedirect(t *testing.T) {
	var unexpected atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { unexpected.Add(1) }))
	defer target.Close()
	for _, name := range []string{"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"} {
		t.Setenv(name, target.URL)
	}
	t.Setenv("NO_PROXY", "")
	t.Setenv("no_proxy", "")
	c := fixture(t, func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL+"/private", http.StatusFound)
	})
	_, err := c.Do(context.Background(), request())
	assertError(t, err, ErrProtocol)
	if unexpected.Load() != 0 {
		t.Fatal("proxy or redirect followed")
	}
	transport := c.http.Transport.(*http.Transport)
	if transport.Proxy != nil || c.http.Timeout != RequestTimeout || transport.ResponseHeaderTimeout != RequestTimeout {
		t.Fatal("unsafe transport settings")
	}
}

func TestProbeAndEnvelopeRejection(t *testing.T) {
	for _, tc := range []struct {
		status                      int
		contentType, encoding, body string
	}{
		{200, "text/plain", "", `{}`}, {200, "application/json", "gzip", `{}`},
		{204, "application/json", "", ``}, {503, "application/json", "", `{"version":"0.1.0","error":"private"}`},
		{200, "application/json", "", `{"version":"0.1.0","status":"ready","transport":"loopback","executes_actions":false,"extra":0}`},
	} {
		c := fixture(t, func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", tc.contentType)
			w.Header().Set("Content-Encoding", tc.encoding)
			w.WriteHeader(tc.status)
			fmt.Fprint(w, tc.body)
		})
		assertError(t, c.Ready(context.Background()), ErrProtocol)
	}
}

func TestBodyDeadlineAndTruncation(t *testing.T) {
	for _, slow := range []bool{false, true} {
		c := fixture(t, func(w http.ResponseWriter, r *http.Request) {
			_, _ = io.Copy(io.Discard, r.Body)
			w.Header().Set("Content-Type", "application/json")
			w.Header().Set("Content-Length", "100")
			fmt.Fprint(w, "{")
			w.(http.Flusher).Flush()
			if slow {
				select {
				case <-r.Context().Done():
				case <-time.After(200 * time.Millisecond):
				}
			}
		})
		ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
		_, err := c.Do(ctx, request())
		cancel()
		want := ErrTransport
		if slow {
			want = ErrDeadline
		}
		assertError(t, err, want)
	}
}

func TestRequestAndResponseBoundaries(t *testing.T) {
	for _, size := range []int{MaxResponseBytes, MaxResponseBytes + 1} {
		c := fixture(t, func(w http.ResponseWriter, r *http.Request) {
			var got Request
			_ = json.NewDecoder(r.Body).Decode(&got)
			if len(got.Text) != 8000 {
				t.Error("maximum text was truncated")
			}
			b := result("draft")
			b["output"] = ""
			raw, _ := json.Marshal(b)
			b["output"] = strings.Repeat("a", size-len(raw))
			raw, _ = json.Marshal(b)
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write(raw)
		})
		r := request()
		r.Text = strings.Repeat("a", 8000)
		got, err := c.Do(context.Background(), r)
		if size == MaxResponseBytes {
			if err != nil || got == nil {
				t.Fatalf("boundary rejected: %v", err)
			}
		} else {
			assertError(t, err, ErrResponseTooLarge)
		}
	}
}

func TestRunMismatchAndUnknownCannotGenerate(t *testing.T) {
	c := fixture(t, func(w http.ResponseWriter, r *http.Request) { reply(w, 200, result("draft")) })
	r := request()
	r.RunID = "different"
	_, err := c.Do(context.Background(), r)
	assertError(t, err, ErrProtocol)
	r = request()
	r.DataClass = Unknown
	_, err = c.Do(context.Background(), r)
	assertError(t, err, ErrProtocol)
	r = request()
	r.DataClass = Restricted
	_, err = c.Do(context.Background(), r)
	assertError(t, err, ErrProtocol)
}

func TestUnicodeEscapes(t *testing.T) {
	for _, tc := range []struct {
		raw   string
		valid bool
	}{
		{`"\ud83d\ude00"`, true}, {`"\\ud800"`, true}, {`"\u0061"`, true},
		{`"\ud800"`, false}, {`"\udc00"`, false}, {`"\ud800\u0041"`, false},
	} {
		c := fixture(t, func(w http.ResponseWriter, r *http.Request) {
			b := result("draft")
			raw, _ := json.Marshal(b)
			body := strings.Replace(string(raw), `"safe draft"`, tc.raw, 1)
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, body)
		})
		_, err := c.Do(context.Background(), request())
		if tc.valid {
			if err != nil {
				t.Fatal(err)
			}
		} else {
			assertError(t, err, ErrProtocol)
		}
	}
}
