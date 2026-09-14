package server

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/harness"
	"go.autonomous.ai/os/system/server/config"
	sensinghttp "go.autonomous.ai/os/system/server/sensing/delivery/http"
)

type voiceRouteTransport struct {
	mutations chan harness.Frame
}

func (f *voiceRouteTransport) Status() harness.Status {
	return harness.Status{Paired: true, Connected: true, MachineID: "computer"}
}

func (f *voiceRouteTransport) Request(_ context.Context, frame harness.Frame) (harness.Frame, error) {
	if frame["type"] == "turn.send" || frame["type"] == "question.answer" {
		f.mutations <- frame
		return harness.Frame{"receipt": map[string]any{"state": "queued"}}, nil
	}
	return harness.Frame{"machineId": "computer", "openQuestion": nil}, nil
}

func TestHarnessVoiceManagementAuthAndSelection(t *testing.T) {
	service, err := harness.NewService(t.TempDir(), harness.Callbacks{})
	if err != nil {
		t.Fatal(err)
	}
	transport := &voiceRouteTransport{mutations: make(chan harness.Frame, 4)}
	s := &Server{config: &config.Config{LLMAPIKey: "owner"}, harnessService: service,
		harnessVoice: harness.NewVoiceController(transport, harness.VoiceCallbacks{})}
	router := gin.New()
	s.registerHarnessRoutes(router.Group("/api"), context.Background())
	call := func(method, path, body, address, token string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.RemoteAddr = address
		r.Header.Set("Content-Type", "application/json")
		if token != "" {
			r.Header.Set("Authorization", "Bearer "+token)
		}
		w := httptest.NewRecorder()
		router.ServeHTTP(w, r)
		return w
	}
	if w := call("GET", "/api/harness/voice-mode", "", "127.0.0.1:50", ""); w.Code != 200 || !strings.Contains(w.Body.String(), `"enabled":false`) {
		t.Fatalf("default mode: %d %s", w.Code, w.Body.String())
	}
	for _, row := range []struct{ method, path string }{
		{"GET", "voice-mode"}, {"PUT", "voice-mode"}, {"GET", "agents"},
		{"GET", "voice-mode/question"}, {"POST", "voice-mode/answer"},
		{"POST", "voice-mode/receipt"}, {"POST", "voice-mode/resolve"},
	} {
		if w := call(row.method, "/api/harness/"+row.path, `{}`, "192.168.1.2:50", ""); w.Code != 401 {
			t.Fatalf("unauthorized %s %s: %d", row.method, row.path, w.Code)
		}
	}
	if w := call("PUT", "/api/harness/voice-mode", `{"enabled":false,"agentId":"mike"}`, "192.168.1.2:50", "owner"); w.Code != 200 {
		t.Fatal(w.Body.String())
	}
	if state := s.harnessVoice.State(); state.Enabled || state.AgentID != "mike" {
		t.Fatalf("selection while disabled: %+v", state)
	}
	if w := call("PUT", "/api/harness/voice-mode", `{"enabled":true,"agentId":"mike"}`, "192.168.1.2:50", "owner"); w.Code != 200 || !s.harnessVoice.State().Enabled {
		t.Fatalf("enable: %s", w.Body.String())
	}
	if w := call("PUT", "/api/harness/voice-mode", `{"agentId":"mike"}`, "192.168.1.2:50", "owner"); w.Code != 400 {
		t.Fatalf("missing enabled must fail: %s", w.Body.String())
	}
}

func TestHarnessVoiceSnapshotDispatchAndIsolation(t *testing.T) {
	transport := &voiceRouteTransport{mutations: make(chan harness.Frame, 4)}
	controller := harness.NewVoiceController(transport, harness.VoiceCallbacks{})
	state, err := controller.SetMode(context.Background(), true, "mike")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	s := &Server{harnessVoice: controller, harnessVoiceCtx: ctx}
	router := gin.New()
	router.POST("/input", func(c *gin.Context) {
		var req sensinghttp.SensingEventRequest
		if err := c.ShouldBindJSON(&req); err != nil {
			t.Error(err)
			return
		}
		if !s.handleHarnessVoice(c, req) {
			c.JSON(200, gin.H{"normal": true})
		}
	})
	call := func(body, remote, forwarded string) *httptest.ResponseRecorder {
		r := httptest.NewRequest("POST", "/input", strings.NewReader(body))
		r.RemoteAddr = remote
		r.Header.Set("Content-Type", "application/json")
		r.Header.Set("X-Forwarded-For", forwarded)
		w := httptest.NewRecorder()
		router.ServeHTTP(w, r)
		return w
	}
	body := fmt.Sprintf(`{"type":"voice_command","message":"Fix reconnect","interaction_id":"capture-1","harness_voice":{"enabled":true,"generation":%d}}`, state.Generation)
	for _, remote := range []string{"192.168.1.2:5", "127.0.0.1:5"} {
		if w := call(body, remote, "192.168.1.2"); w.Code != 403 {
			t.Fatalf("remote/proxied direct input admitted: %d", w.Code)
		}
	}
	w := call(body, "127.0.0.1:5", "")
	if w.Code != 200 {
		t.Fatal(w.Body.String())
	}
	var response struct {
		Data struct {
			RunID string `json:"runId"`
		} `json:"data"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &response); err != nil || response.Data.RunID != newHarnessVoiceRunID("capture-1") {
		t.Fatalf("stable response route: %s", w.Body.String())
	}
	select {
	case frame := <-transport.mutations:
		if frame["text"] != "Fix reconnect" || frame["agentId"] != "mike" || frame["response"] != nil {
			t.Fatalf("wire payload: %+v", frame)
		}
	case <-time.After(time.Second):
		t.Fatal("voice was not sent")
	}
	for _, body := range []string{
		`{"type":"web_chat","message":"Hello"}`,
		`{"type":"mqtt_chat","message":"Hello"}`,
		`{"type":"voice","message":"MQTT speak request"}`,
		`{"type":"voice_agent_handled","message":"already answered"}`,
		`{"type":"presence.enter","message":"Someone arrived"}`,
	} {
		if w := call(body, "127.0.0.1:5", ""); !strings.Contains(w.Body.String(), `"normal":true`) {
			t.Fatalf("unrelated input rerouted: %s", w.Body.String())
		}
	}
}

func TestHarnessVoiceQuestionUsesCLIShapedPromptAndOptions(t *testing.T) {
	frame := harness.Frame{"payload": map[string]any{"questions": []any{
		map[string]any{"key": "theme", "q": "Which theme?", "options": []any{"Blue", "Red"}, "multi": false},
		map[string]any{"key": "size", "q": "Which size?", "options": []any{"Small", "Large"}},
	}}}
	if got := harnessEventText("question.open", frame); got != "Which theme?\nBlue\nRed" {
		t.Fatalf("first spoken question = %q", got)
	}
}
