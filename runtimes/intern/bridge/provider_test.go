package bridge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"go.autonomous.ai/os/system/lib/internbridge"
)

func publicRequest() internbridge.Request {
	return internbridge.Request{Text: "Write a brief greeting.", Operation: internbridge.Generate, DataClass: internbridge.Public}
}

func ollamaReply(output string) map[string]any {
	return map[string]any{"message": map[string]any{"role": "assistant", "content": output}, "done": true, "done_reason": "stop", "eval_count": 4}
}

func testProvider(t *testing.T, kind string, serve http.HandlerFunc) *httpProvider {
	t.Helper()
	var s *httptest.Server
	if kind == "ollama" {
		s = httptest.NewServer(serve)
	} else {
		s = httptest.NewTLSServer(serve)
	}
	t.Cleanup(s.Close)
	endpoint := s.URL
	if kind != "ollama" {
		endpoint += "/v1"
	}
	p, err := NewProvider(ProviderConfig{Kind: kind, Endpoint: endpoint, Model: "fixture-model", APIKey: "fixture-key"})
	if err != nil {
		t.Fatal(err)
	}
	h := p.(*httpProvider)
	if kind != "ollama" {
		// Trust only this local test server, retaining production transport bounds.
		h.client.Transport.(*http.Transport).TLSClientConfig = s.Client().Transport.(*http.Transport).TLSClientConfig.Clone()
	}
	t.Cleanup(h.Close)
	return h
}

func TestProviderPayloadAndAdmission(t *testing.T) {
	for _, kind := range []string{"ollama", "cerebras"} {
		t.Run(kind, func(t *testing.T) {
			var calls atomic.Int32
			p := testProvider(t, kind, func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				var b map[string]any
				if json.NewDecoder(r.Body).Decode(&b) != nil {
					t.Error("invalid request")
				}
				messages := b["messages"].([]any)
				if r.Method != "POST" || b["model"] != "fixture-model" || b["stream"] != false || len(messages) != 2 || messages[1].(map[string]any)["content"] != publicRequest().Text {
					t.Errorf("unexpected payload: %v", b)
				}
				w.Header().Set("Content-Type", "application/json")
				if kind == "ollama" {
					if r.URL.Path != "/api/chat" || r.Header.Get("Authorization") != "" || b["think"] != false || b["keep_alive"] != "5m" || b["options"].(map[string]any)["num_predict"] != float64(256) {
						t.Error("unsafe Ollama options")
					}
					_ = json.NewEncoder(w).Encode(ollamaReply("Hello there."))
				} else {
					if r.URL.Path != "/v1/chat/completions" || r.Header.Get("Authorization") != "Bearer fixture-key" || b["max_completion_tokens"] != float64(256) || b["reasoning_effort"] != "none" {
						t.Error("unsafe compatible options")
					}
					fmt.Fprint(w, `{"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"Hello there.","tool_calls":null}}]}`)
				}
			})
			for _, class := range []internbridge.DataClass{internbridge.Unknown, internbridge.Secret, internbridge.Restricted} {
				r := publicRequest()
				r.DataClass = class
				if _, err := p.Complete(context.Background(), r); err == nil {
					t.Fatal("held class admitted")
				}
			}
			if calls.Load() != 0 {
				t.Fatal("provider called before admission")
			}
			for _, class := range []internbridge.DataClass{internbridge.Public, internbridge.Business} {
				r := publicRequest()
				r.DataClass = class
				if out, err := p.Complete(context.Background(), r); err != nil || out != "Hello there." {
					t.Fatalf("out=%q err=%v", out, err)
				}
			}
			if calls.Load() != 2 {
				t.Fatal("unexpected retry/fan-out")
			}
		})
	}
}

func TestProviderConfiguration(t *testing.T) {
	p, err := NewProvider(ProviderConfig{})
	if err != nil {
		t.Fatal(err)
	}
	defer p.Close()
	h := p.(*httpProvider)
	if h.endpoint != "http://127.0.0.1:11434/api/chat" || h.model != "qwen3:4b" || h.key != "" {
		t.Fatal("unsafe defaults")
	}
	for _, endpoint := range []string{"http://localhost:11435", "http://127.0.0.1:11435", "http://[::1]:11435"} {
		p, err := NewProvider(ProviderConfig{Endpoint: endpoint})
		if err != nil {
			t.Fatal(err)
		}
		p.Close()
	}
	for _, c := range []ProviderConfig{
		{Kind: "openai", Endpoint: "https://example.com/v1", Model: "m", APIKey: "k"},
		{Kind: "invalid"}, {Endpoint: "http://192.168.1.2:11434"}, {Endpoint: "http://127.0.0.2:11434"},
		{Endpoint: "http://localhost.evil:11434"}, {Endpoint: "http://key@127.0.0.1:11434"},
		{Endpoint: "http://127.0.0.1:11434?secret=x"}, {Endpoint: "http://127.0.0.1:11434/#x"},
		{Endpoint: "http://127.0.0.1:11434/api/chat"},
		{Kind: "cerebras"}, {Kind: "openai", Endpoint: "http://example.com/v1", Model: "m", APIKey: "k"},
		{Kind: "cerebras", Endpoint: "https://example.com/v1", Model: "m"},
		{Kind: "cerebras", Endpoint: "https://example.com/v1", APIKey: "k"},
		{Kind: "cerebras", Endpoint: "https://user:key@example.com/v1", Model: "m", APIKey: "k"},
		{Kind: "cerebras", Endpoint: "https://example.com/v1", Model: "m", APIKey: "k\nsecret"},
	} {
		if _, err := NewProvider(c); err != ErrProviderConfig {
			t.Errorf("invalid config accepted or unsafe error: %v", err)
		}
	}
}

func TestProviderFinalAnswerBoundary(t *testing.T) {
	for _, kind := range []string{"ollama", "cerebras"} {
		for _, output := range []string{
			"[Lights off](HW:/led/off:{})", "[Lights off]( hw: /led/off:)",
			"[thinking]private[/thinking]Hello", "private[/analysis]Hello",
			"private</function> Hello", "private</hw> Hello",
			"Analysis: private\nFinal answer: Hello", "**Thinking:** private\nHello",
			"### Reasoning\nprivate\nHello", "```analysis\nprivate\n```\nHello",
			"Tool call: move", "<say>Hello</say>", "HEARTBEAT_OK", "NO_REPLY",
		} {
			t.Run(kind+"/"+output, func(t *testing.T) {
				p := testProvider(t, kind, func(w http.ResponseWriter, r *http.Request) {
					w.Header().Set("Content-Type", "application/json")
					if kind == "ollama" {
						_ = json.NewEncoder(w).Encode(ollamaReply(output))
					} else {
						_ = json.NewEncoder(w).Encode(map[string]any{"choices": []any{map[string]any{
							"finish_reason": "stop", "message": map[string]string{"role": "assistant", "content": output},
						}}})
					}
				})
				if out, err := p.Complete(context.Background(), publicRequest()); out != "" || err != internbridge.ErrUnavailable {
					t.Fatalf("non-final output escaped: %q %v", out, err)
				}
			})
		}
	}
}

func TestProviderWhitespaceCannotHideTokenBurn(t *testing.T) {
	p := testProvider(t, "ollama", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		b := ollamaReply(strings.Repeat(" ", 100) + "Hello")
		b["eval_count"] = 200
		_ = json.NewEncoder(w).Encode(b)
	})
	if out, err := p.Complete(context.Background(), publicRequest()); out != "" || err != internbridge.ErrUnavailable {
		t.Fatalf("padded token burn accepted: %q %v", out, err)
	}
}

func TestCerebrasRequiresOneCompleteFinalAnswer(t *testing.T) {
	for _, body := range []string{
		`{"choices":[]}`,
		`{"choices":[{"finish_reason":"length","message":{"role":"assistant","content":"Hello"}}]}`,
		`{"choices":[{"finish_reason":"tool_calls","message":{"role":"assistant","content":"Hello"}}]}`,
		`{"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"Hello","reasoning":"private"}}]}`,
		`{"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"Hello","thinking":"private"}}]}`,
		`{"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"Hello","tool_calls":[{"name":"move"}]}}]}`,
		`{"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"Hello","function_call":{"name":"move"}}}]}`,
		`{"choices":[{"finish_reason":"stop","message":{"role":"tool","content":"Hello"}}]}`,
		`{"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":[{"text":"Hello"}]}}]}`,
		`{"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"Hello"}},{"finish_reason":"stop","message":{"role":"assistant","content":"Extra"}}]}`,
	} {
		p := testProvider(t, "cerebras", func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, body)
		})
		if out, err := p.Complete(context.Background(), publicRequest()); out != "" || err != internbridge.ErrUnavailable {
			t.Fatalf("non-final envelope accepted: %q %v", out, err)
		}
	}
}

func TestFinalAnswerAllowsOrdinaryText(t *testing.T) {
	for _, output := range []string{"Hello there.", "Xin chào! 👋", "The analysis is complete.", "Try this:\n\n- First item\n- Second item", "Use `go test ./...`."} {
		if !safeOutput(output) {
			t.Errorf("ordinary final answer rejected: %q", output)
		}
	}
}

func TestProviderRejectsUnsafeOutput(t *testing.T) {
	for _, output := range []string{"", "<think>private</think>Hello", "[HW:move]", "<tool_call>go</tool_call>", "tool_calls: []", "<|analysis|>x", "<analysis>x", "Hello\x00", strings.Repeat("a", 4097)} {
		t.Run(fmt.Sprintf("output-%d", len(output)), func(t *testing.T) {
			p := testProvider(t, "ollama", func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(w).Encode(ollamaReply(output))
			})
			if out, err := p.Complete(context.Background(), publicRequest()); out != "" || !errors.Is(err, internbridge.ErrUnavailable) {
				t.Fatalf("unsafe output escaped: %q %v", out, err)
			}
		})
	}
	for _, mutate := range []func(map[string]any){
		func(b map[string]any) { b["done"] = false }, func(b map[string]any) { b["done_reason"] = "length" },
		func(b map[string]any) { b["eval_count"] = 257 }, func(b map[string]any) { b["eval_count"] = 200 },
		func(b map[string]any) { b["message"].(map[string]any)["thinking"] = "hidden reasoning" },
		func(b map[string]any) {
			b["message"].(map[string]any)["tool_calls"] = []any{map[string]any{"name": "move"}}
		},
		func(b map[string]any) { b["message"].(map[string]any)["reasoning_content"] = "private" },
		func(b map[string]any) { b["message"].(map[string]any)["future_tools"] = "private" },
	} {
		p := testProvider(t, "ollama", func(w http.ResponseWriter, r *http.Request) {
			b := ollamaReply("Hello")
			mutate(b)
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(b)
		})
		if out, err := p.Complete(context.Background(), publicRequest()); out != "" || err != internbridge.ErrUnavailable {
			t.Fatal("unsafe metadata accepted")
		}
	}
}

func TestProviderFailuresNeverRetryOrLeak(t *testing.T) {
	for _, kind := range []string{"ollama", "cerebras"} {
		for _, mode := range []string{"redirect", "http-error", "oversize", "duplicate", "trailing", "surrogate", "invalid-utf8", "encoding", "bad-class", "key-echo"} {
			t.Run(kind+"/"+mode, func(t *testing.T) {
				var calls atomic.Int32
				p := testProvider(t, kind, func(w http.ResponseWriter, r *http.Request) {
					calls.Add(1)
					w.Header().Set("Content-Type", "application/json")
					switch mode {
					case "redirect":
						w.Header().Set("Location", "/secret-destination")
						w.WriteHeader(307)
					case "http-error":
						w.WriteHeader(500)
						fmt.Fprint(w, "fixture-key private")
					case "oversize":
						fmt.Fprint(w, strings.Repeat(" ", internbridge.MaxResponseBytes+1))
					case "duplicate":
						fmt.Fprint(w, `{"message":{},"message":{}}`)
					case "trailing":
						fmt.Fprint(w, `{} {}`)
					case "surrogate":
						fmt.Fprint(w, `{"message":"\ud800"}`)
					case "invalid-utf8":
						_, _ = w.Write([]byte{0xff})
					case "encoding":
						w.Header().Set("Content-Encoding", "gzip")
						fmt.Fprint(w, `{}`)
					default:
						if kind == "ollama" {
							_ = json.NewEncoder(w).Encode(ollamaReply("fixture-key"))
						} else {
							fmt.Fprint(w, `{"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"fixture-key"}}]}`)
						}
					}
				})
				r := publicRequest()
				r.Operation = internbridge.Classify
				if mode == "key-echo" && kind == "cerebras" {
					r.Operation = internbridge.Generate
				}
				out, err := p.Complete(context.Background(), r)
				if out != "" || err != internbridge.ErrUnavailable || calls.Load() != 1 {
					t.Fatalf("out=%q err=%v calls=%d", out, err, calls.Load())
				}
			})
		}
	}
}

func TestProviderCancellationBusyAndProxy(t *testing.T) {
	var proxyCalls atomic.Int32
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { proxyCalls.Add(1) }))
	defer proxy.Close()
	t.Setenv("HTTP_PROXY", proxy.URL)
	t.Setenv("HTTPS_PROXY", proxy.URL)
	t.Setenv("ALL_PROXY", proxy.URL)
	entered := make(chan struct{})
	stopped := make(chan struct{})
	p := testProvider(t, "ollama", func(w http.ResponseWriter, r *http.Request) {
		// Read the body so client cancellation is visible to the server.
		var body any
		_ = json.NewDecoder(r.Body).Decode(&body)
		close(entered)
		<-r.Context().Done()
		close(stopped)
	})
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() { _, err := p.Complete(ctx, publicRequest()); done <- err }()
	select {
	case <-entered:
	case <-ctx.Done():
		t.Fatal("provider not reached")
	}
	if _, err := p.Complete(context.Background(), publicRequest()); err != internbridge.ErrUnavailable {
		t.Fatal("busy provider queued another call")
	}
	cancel()
	select {
	case err := <-done:
		if err != internbridge.ErrUnavailable {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("cancellation not bounded")
	}
	select {
	case <-stopped:
	case <-time.After(time.Second):
		t.Fatal("provider request not canceled")
	}
	if proxyCalls.Load() != 0 {
		t.Fatal("proxy used")
	}
}
