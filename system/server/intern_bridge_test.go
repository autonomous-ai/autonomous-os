package server

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"go.autonomous.ai/os/runtimes/intern"
	"go.autonomous.ai/os/runtimes/intern/bridge"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/internbridge"
	"go.autonomous.ai/os/system/server/config"
)

func waitInternTurn(t *testing.T, worker *intern.Service, id string) intern.Turn {
	t.Helper()
	deadline := time.After(3 * time.Second)
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-deadline:
			t.Fatal("turn did not finish")
		case <-ticker.C:
			turn, err := worker.Result(id)
			if err != nil {
				t.Fatal(err)
			}
			if turn.State != "queued" && turn.State != "running" {
				return turn
			}
		}
	}
}

func assertBridgeReleased(t *testing.T) {
	t.Helper()
	l, err := net.Listen("tcp4", bridge.Address)
	if err != nil {
		t.Fatalf("bridge listener not released: %v", err)
	}
	_ = l.Close()
}

func TestNativeInternLifecycleAndGeneration(t *testing.T) {
	var calls atomic.Int32
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path != "/api/chat" || r.Header.Get("Authorization") != "" {
			t.Error("unexpected provider request")
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"message": map[string]string{"role": "assistant", "content": "Hello from the fixture."}, "done": true, "done_reason": "stop", "eval_count": 5})
	}))
	defer provider.Close()
	cfg := &config.Config{AgentRuntime: "intern", InternOllamaURL: provider.URL, InternOllamaModel: "fixture", LLMAPIKey: "admin-fixture"}
	s := newInternServer(cfg)
	worker := s.agentGateway.(*intern.Service)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- s.runIntern(ctx) }()
	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(6 * time.Second):
			t.Error("lifecycle did not stop")
		}
	})
	waitCtx, stop := context.WithTimeout(ctx, 3*time.Second)
	defer stop()
	if err := worker.WaitStarted(waitCtx); err != nil {
		t.Fatal(err)
	}
	client, _ := internbridge.New(intern.BridgePort)
	defer client.CloseIdleConnections()
	if err := client.Ready(ctx); err != nil {
		t.Fatal(err)
	}
	if calls.Load() != 0 || worker.IsReady() {
		t.Fatal("startup/health inferred readiness")
	}
	// The actual administrator route feeds the unchanged worker, native bridge,
	// and local provider, then exposes the correlated terminal result.
	router := s.internRouter()
	rec := internRequest(t, router, "POST", "/api/agent/intern/chat", `{"text":"Write a greeting.","operation":"generate","data_class":"public","admission":"administrator_classified_exact_text"}`, "admin-fixture")
	if rec.Code != 202 {
		t.Fatal(rec.Code, rec.Body.String())
	}
	var admitted struct {
		Data struct {
			RunID string `json:"run_id"`
		} `json:"data"`
	}
	_ = json.Unmarshal(rec.Body.Bytes(), &admitted)
	turn := waitInternTurn(t, worker, admitted.Data.RunID)
	if turn.State != "completed" || turn.Result == nil || turn.Result.Output != "Hello from the fixture." || !worker.IsReady() || calls.Load() != 1 {
		t.Fatalf("bad terminal proof: %+v", turn)
	}
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
		done <- nil
	case <-time.After(6 * time.Second):
		t.Fatal("shutdown hung")
	}
	if worker.IsReady() {
		t.Fatal("readiness survived shutdown")
	}
	assertBridgeReleased(t)
}

func TestNativeInternStartupFailuresAndInactiveRuntime(t *testing.T) {
	l, err := net.Listen("tcp4", bridge.Address)
	if err != nil {
		t.Fatal(err)
	}
	s := newInternServer(&config.Config{AgentRuntime: "intern"})
	if err := s.runIntern(context.Background()); err != bridge.ErrListener {
		t.Fatalf("occupied bridge adopted: %v", err)
	}
	// A different active runtime never tries to bind or initialize providers,
	// regardless of a pending saved Intern selection or invalid provider config.
	s.agentGateway = &selectionGateway{}
	s.config.InternProvider = "invalid"
	if err := s.runIntern(context.Background()); !errors.Is(err, domain.ErrNotSupportedByRuntime) {
		t.Fatal(err)
	}
	_ = l.Close()
	s = newInternServer(&config.Config{AgentRuntime: "intern", HttpPort: int(intern.BridgePort)})
	if err := s.runIntern(context.Background()); err == nil {
		t.Fatal("admin port collision ignored")
	}
	assertBridgeReleased(t)
	s = newInternServer(&config.Config{AgentRuntime: "intern", InternProvider: "cerebras"})
	if err := s.runIntern(context.Background()); err != bridge.ErrProviderConfig {
		t.Fatal("incomplete remote config did not fail closed", err)
	}
	assertBridgeReleased(t)
}

func TestNativeInternShutdownCancelsProvider(t *testing.T) {
	entered, stopped := make(chan struct{}), make(chan struct{})
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body any
		_ = json.NewDecoder(r.Body).Decode(&body)
		close(entered)
		<-r.Context().Done()
		close(stopped)
	}))
	defer provider.Close()
	s := newInternServer(&config.Config{AgentRuntime: "intern", InternOllamaURL: provider.URL})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- s.runIntern(ctx) }()
	worker := s.agentGateway.(*intern.Service)
	waitCtx, stop := context.WithTimeout(ctx, 3*time.Second)
	defer stop()
	if err := worker.WaitStarted(waitCtx); err != nil {
		t.Fatal(err)
	}
	a, err := intern.AdmitTrustedRequest(internbridge.Request{Text: "hello", Operation: internbridge.Generate, DataClass: internbridge.Business})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := worker.Submit(a); err != nil {
		t.Fatal(err)
	}
	select {
	case <-entered:
	case <-waitCtx.Done():
		t.Fatal("provider not called")
	}
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(6 * time.Second):
		t.Fatal("shutdown hung")
	}
	select {
	case <-stopped:
	case <-time.After(time.Second):
		t.Fatal("provider not canceled")
	}
	assertBridgeReleased(t)
}

func TestNativeInternProviderConfigIsExplicit(t *testing.T) {
	cfg := &config.Config{LLMBaseURL: "https://configured.invalid/v1", LLMModel: "remote-model", LLMAPIKey: "fixture-key"}
	s := &Server{config: cfg}
	got := s.internProviderConfig()
	if got.Kind != "ollama" || got.Endpoint != "" || got.Model != "" || got.APIKey != "" {
		t.Fatal("inherited LLM enabled cloud")
	}
	cfg.InternProvider = "cerebras"
	got = s.internProviderConfig()
	if got.Kind != "cerebras" || got.Endpoint != cfg.LLMBaseURL || got.Model != cfg.LLMModel || got.APIKey != cfg.LLMAPIKey {
		t.Fatal("explicit provider config not used")
	}
	// Selection is explicit and exhaustive. Unsupported providers cannot fall
	// through to generic OpenAI compatibility or silently revert to Ollama.
	cfg.InternProvider = "openai"
	if _, err := bridge.NewProvider(s.internProviderConfig()); err != bridge.ErrProviderConfig {
		t.Fatal("unsupported provider accepted", err)
	}
	cfg.InternProvider = "ollama"
	cfg.InternOllamaURL = "http://localhost:11435"
	cfg.InternOllamaModel = "local-fixture"
	got = s.internProviderConfig()
	if got.Endpoint != cfg.InternOllamaURL || got.Model != cfg.InternOllamaModel || got.APIKey != "" {
		t.Fatal("explicit local config mixed with remote settings")
	}
}
