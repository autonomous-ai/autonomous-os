package server

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/runtimes/intern"
	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/internbridge"
	"go.autonomous.ai/os/system/server/config"
	devicehttp "go.autonomous.ai/os/system/server/device/delivery/http"
)

func TestInternHTTPAdmissionBridgeResultEndToEnd(t *testing.T) {
	listener, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", intern.BridgePort))
	if err != nil {
		t.Fatalf("listen on fixed Intern bridge: %v", err)
	}
	defer listener.Close()

	var bridgeCalls atomic.Int32
	bridge := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		bridgeCalls.Add(1)
		if r.Method != http.MethodPost || r.URL.Path != "/v1/intern" || r.Host != fmt.Sprintf("127.0.0.1:%d", intern.BridgePort) {
			t.Errorf("unexpected bridge request: %s %s host=%q", r.Method, r.URL.Path, r.Host)
		}
		if r.Header.Get("Authorization") != "" || r.Header.Get("Cookie") != "" {
			t.Error("credentials reached bridge")
		}
		var req internbridge.Request
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Fatal(err)
		}
		if req.Text != "hello Welcome Desk" || req.Operation != internbridge.Generate || req.DataClass != internbridge.Public {
			t.Errorf("admission changed before bridge: %+v", req)
		}
		hash := sha256.Sum256([]byte(req.RunID))
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"version": internbridge.Version, "response_schema": internbridge.ResponseSchema,
			"run_id": "run-" + hex.EncodeToString(hash[:12]), "destination": internbridge.FirstContact,
			"requested_destination": "rex@dru", "kind": "persona", "status": "draft",
			"output": "Cassi-first draft", "executes_actions": false, "transport_status": "accepted",
			"lifecycle_status": "completed", "lifecycle_scope": "bridge_request",
			"reception_route": map[string]any{"first_destination": internbridge.FirstContact, "handoff": "rex@dru", "intent": "engineering", "status": "reception_route", "executed": false, "next_step": "safe_escalation"},
		})
	})}
	go func() { _ = bridge.Serve(listener) }()
	defer bridge.Shutdown(context.Background())

	cfg := &config.Config{AgentRuntime: domain.AgentRuntimeIntern, LLMAPIKey: "admin-only-test-token"}
	s := newInternServer(cfg)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		s.agentGateway.StartWS(ctx, func(context.Context, domain.WSEvent) error {
			t.Error("device event callback called")
			return nil
		})
	}()
	defer func() { cancel(); <-done }()
	worker := s.agentGateway.(*intern.Service)
	startCtx, cancelStart := context.WithTimeout(context.Background(), time.Second)
	defer cancelStart()
	if err := worker.WaitStarted(startCtx); err != nil {
		t.Fatalf("wait for Intern worker: %v", err)
	}

	router := s.internRouter()
	body := `{"text":"hello Welcome Desk","operation":"generate","data_class":"public","admission":"administrator_classified_exact_text"}`
	rec := internRequest(t, router, http.MethodPost, "/api/agent/intern/chat", body, "admin-only-test-token")
	if rec.Code != http.StatusAccepted {
		t.Fatalf("admission status=%d body=%s", rec.Code, rec.Body.String())
	}
	var admitted struct {
		Data struct {
			RunID string `json:"run_id"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &admitted); err != nil || admitted.Data.RunID == "" {
		t.Fatalf("decode admission: %v body=%s", err, rec.Body.String())
	}

	deadline := time.Now().Add(2 * time.Second)
	for {
		rec = internRequest(t, router, http.MethodGet, "/api/agent/intern/result/"+admitted.Data.RunID, "", "admin-only-test-token")
		if rec.Code != http.StatusOK {
			t.Fatalf("result status=%d body=%s", rec.Code, rec.Body.String())
		}
		if strings.Contains(rec.Body.String(), `"state":"completed"`) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("result did not complete: %s", rec.Body.String())
		}
		time.Sleep(time.Millisecond)
	}
	if !strings.Contains(rec.Body.String(), `"output":"Cassi-first draft"`) || !strings.Contains(rec.Body.String(), `"executes_actions":false`) {
		t.Fatalf("unsafe or missing result: %s", rec.Body.String())
	}
	if bridgeCalls.Load() != 1 {
		t.Fatalf("bridge calls=%d, want exactly 1", bridgeCalls.Load())
	}
}

type selectionGateway struct{ domain.AgentGateway }

func (*selectionGateway) Name() string  { return "openclaw" }
func (*selectionGateway) IsReady() bool { return true }

func selectionConfig(t *testing.T, runtime string) *config.Config {
	t.Helper()
	t.Chdir(t.TempDir())
	if err := os.Mkdir("config", 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile("config/config.json", []byte(fmt.Sprintf(`{"agent_runtime":%q,"llm_api_key":"test-admin"}`, runtime)), 0600); err != nil {
		t.Fatal(err)
	}
	cfg, err := config.Load()
	if err != nil {
		t.Fatal(err)
	}
	return cfg
}

func assertSelectionStatus(t *testing.T, rec *httptest.ResponseRecorder, active, selected string, ready bool) {
	t.Helper()
	var response struct {
		Data map[string]any `json:"data"`
	}
	if rec.Code != 200 || json.Unmarshal(rec.Body.Bytes(), &response) != nil {
		t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
	}
	d := response.Data
	if d["current"] != active || d["active"] != active || d["selected"] != selected || d["ready"] != ready || d["readiness_runtime"] != active || d["restart_required"] != (active != selected) {
		t.Fatalf("incorrect selected/active status: %v", d)
	}
}

func TestInternSelectionKeepsActiveDeviceStatus(t *testing.T) {
	cfg := selectionConfig(t, "openclaw")
	gw := &selectionGateway{}
	ds := device.ProvideService(cfg, nil, gw, nil, nil)
	h := devicehttp.ProvideDeviceHandler(ds, nil, cfg)
	router := gin.New()
	router.GET("/status", h.GetAgentRuntime)
	router.POST("/select", h.SetAgentRuntime)
	rec := internRequest(t, router, "POST", "/select", `{"runtime":"intern"}`, "")
	assertSelectionStatus(t, rec, "openclaw", "intern", true)
	rec = internRequest(t, router, "GET", "/status", "", "")
	assertSelectionStatus(t, rec, "openclaw", "intern", true)
	// Exercise actual HTTP reads while persistence updates the saved selection.
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < 100; i++ {
			rec := internRequest(t, router, "GET", "/status", "", "")
			if rec.Code != 200 {
				t.Errorf("concurrent status: %d", rec.Code)
			}
		}
	}()
	for i := 0; i < 20; i++ {
		target := "intern"
		if i%2 == 0 {
			target = "openclaw"
		}
		if err := cfg.SelectAgentRuntime(target); err != nil {
			t.Error(err)
		}
	}
	wg.Wait()
}

func TestInternSelectionLeavingDoesNotDeactivateAdmission(t *testing.T) {
	cfg := selectionConfig(t, "intern")
	s := newInternServer(cfg)
	router := s.internRouter()
	rec := internRequest(t, router, "POST", "/api/device/agent-runtime", `{"runtime":"openclaw"}`, "test-admin")
	assertSelectionStatus(t, rec, "intern", "openclaw", false)
	rec = internRequest(t, router, "GET", "/api/device/agent-runtime", "", "test-admin")
	assertSelectionStatus(t, rec, "intern", "openclaw", false)
	// No worker/network: a valid admission must reach Submit (503), not the
	// inactive-gateway guard (409), despite the pending selection.
	rec = internRequest(t, router, "POST", "/api/agent/intern/chat", `{"text":"hello","operation":"generate","data_class":"public","admission":"administrator_classified_exact_text"}`, "test-admin")
	if rec.Code != 503 || !strings.Contains(rec.Body.String(), intern.ErrNotStarted.Error()) {
		t.Fatalf("pending selection deactivated admission: %d %s", rec.Code, rec.Body.String())
	}
	if rec := internRequest(t, router, "POST", "/api/agent/restart", `{}`, "test-admin"); rec.Code != 501 {
		t.Fatalf("device route enabled before restart: %d", rec.Code)
	}
}

func TestInternSurrogateAdmission(t *testing.T) {
	s := newInternServer(&config.Config{AgentRuntime: "intern", LLMAPIKey: "test-admin"})
	router := s.internRouter()
	for _, tc := range []struct {
		name, text, decoded string
		valid               bool
	}{
		{"high", `\ud800`, "", false},
		{"low", `\udc00`, "", false},
		{"high_then_ascii", `\ud800\u0041`, "", false},
		{"pair", `\ud83d\ude00`, "😀", true},
		{"literal_escape", `\\ud800`, `\ud800`, true},
		{"replacement_character", `\ufffd`, "�", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			body := `{"text":"` + tc.text + `","operation":"generate","data_class":"public","admission":"administrator_classified_exact_text"}`
			rec := internRequest(t, router, "POST", "/api/agent/intern/chat", body, "test-admin")
			want := 400
			if tc.valid {
				want = 503
			} // validated, reaches the unstarted worker; no network
			if rec.Code != want {
				t.Fatalf("status=%d want=%d body=%s", rec.Code, want, rec.Body.String())
			}
			if tc.valid {
				ctx, _ := gin.CreateTestContext(httptest.NewRecorder())
				ctx.Request = httptest.NewRequest("POST", "/", strings.NewReader(`{"text":"`+tc.text+`"}`))
				var decoded struct {
					Text string `json:"text"`
				}
				if err := decodeInternBody(ctx, &decoded); err != nil || decoded.Text != tc.decoded {
					t.Fatalf("text changed: %q err=%v", decoded.Text, err)
				}
			}
		})
	}
}

func TestInitializeInternServerUsesMinimalActivationGraph(t *testing.T) {
	dir := t.TempDir()
	t.Chdir(dir)
	if err := os.MkdirAll(filepath.Join(dir, "config"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "config", "config.json"), []byte(`{"agent_runtime":"intern","http_port":0,"llm_api_key":"admin"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	s, selected, err := InitializeInternServer()
	if err != nil || !selected || s == nil {
		t.Fatalf("activation server=%v selected=%v err=%v", s, selected, err)
	}
	if s.agentGateway == nil || s.agentGateway.Name() != domain.AgentRuntimeIntern || s.config.HttpPort != config.Default().HttpPort {
		t.Fatalf("wrong activation: gateway=%T port=%d", s.agentGateway, s.config.HttpPort)
	}
	if !reflect.ValueOf(s.networkHandler).IsZero() || s.sensingHandler != nil || !reflect.ValueOf(s.deviceHandler).IsZero() || s.agentHandler != nil || !reflect.ValueOf(s.deviceMQTTHandler).IsZero() {
		t.Fatal("Intern activation constructed device/channel handlers")
	}
}

func TestInternServerRejectsDeviceChannelAndUnclassifiedEffects(t *testing.T) {
	cfg := &config.Config{AgentRuntime: domain.AgentRuntimeIntern, LLMAPIKey: "admin-only-test-token"}
	s := newInternServer(cfg)
	router := s.internRouter()

	for _, tc := range []struct{ method, path, body string }{
		{http.MethodPost, "/api/device/channel", `{"type":"telegram","token":"must-not-load"}`},
		{http.MethodPost, "/api/hardware/led", `{}`},
		{http.MethodPost, "/api/agent/events", `{"type":"tool","text":"act"}`},
		{http.MethodPost, "/api/agent/intern/chat", `{"text":"unclassified","operation":"generate","data_class":"public"}`},
		{http.MethodPost, "/api/agent/intern/chat", `{"text":"secret","operation":"generate","data_class":"secret","admission":"administrator_classified_exact_text"}`},
	} {
		rec := internRequest(t, router, tc.method, tc.path, tc.body, "admin-only-test-token")
		if tc.path == "/api/agent/intern/chat" {
			if rec.Code != http.StatusBadRequest {
				t.Errorf("%s status=%d body=%s", tc.path, rec.Code, rec.Body.String())
			}
		} else if rec.Code != http.StatusNotImplemented {
			t.Errorf("%s status=%d body=%s", tc.path, rec.Code, rec.Body.String())
		}
	}
	if cfg.AgentRuntime != domain.AgentRuntimeIntern || cfg.TelegramBotToken != "" {
		t.Fatalf("restricted requests mutated config: %+v", cfg)
	}
}

func internRequest(t *testing.T, handler http.Handler, method, path, body, token string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	return rec
}
