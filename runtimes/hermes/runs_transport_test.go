package hermes

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"go.autonomous.ai/os/system/domain"
)

func managedTestService(t *testing.T, handler http.HandlerFunc) *HermesService {
	t.Helper()
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	oldURL, oldKey := BaseURL, APIKey
	BaseURL, APIKey = server.URL, "test-key"
	t.Cleanup(func() { BaseURL, APIKey = oldURL, oldKey })
	return &HermesService{httpClient: server.Client()}
}

func TestManagedRunAdmissionUsesSessionAndRejectsEmptyContent(t *testing.T) {
	calls := 0
	s := managedTestService(t, func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Header.Get("Authorization") != "Bearer test-key" {
			t.Error("missing auth")
		}
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		if body["session_id"] != "session-old" || body["input"] != "hello" || body["instructions"] != "soul" {
			t.Errorf("body %#v", body)
		}
		if _, ok := body["conversation"]; ok {
			t.Error("Responses chain incorrectly sent")
		}
		w.WriteHeader(202)
		fmt.Fprint(w, `{"run_id":"run-1","status":"started"}`)
	})
	id, err := s.createManagedRun(context.Background(), streamRequest{Input: "hello", Instructions: "soul", Conversation: "old-resp"}, "session-old")
	if err != nil || id != "run-1" {
		t.Fatalf("%q %v", id, err)
	}
	_, err = s.createManagedRun(context.Background(), streamRequest{Input: []inputMessage{{Role: "user"}}}, "")
	if err == nil || calls != 1 {
		t.Fatalf("empty content admitted: calls=%d err=%v", calls, err)
	}
}

func TestManagedRunCapabilitiesAndControlRejection(t *testing.T) {
	s := managedTestService(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/capabilities" {
			fmt.Fprint(w, `{"features":{"run_submission":true,"run_events_sse":true,"run_status":true,"run_stop":true,"run_steer":true,"runs_idempotency":{"autonomous_run_events_v1":true}}}`)
			return
		}
		w.WriteHeader(409)
		fmt.Fprint(w, `{"error":{"code":"run_not_accepting_steer"}}`)
	})
	if !s.discoverRunSteering(context.Background()) {
		t.Fatal("missing supported capabilities")
	}
	var status *managedRunHTTPError
	if err := s.controlManagedRun(context.Background(), "run-1", "steer", "again"); !errors.As(err, &status) || status.StatusCode != 409 {
		t.Fatalf("untyped rejection: %v", err)
	}
}

func TestManagedRunNativeStreamPreservesOwnerAndPendingSteer(t *testing.T) {
	s := managedTestService(t, func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasSuffix(r.URL.Path, "/events") {
			fmt.Fprint(w, `{"run_id":"run-1","status":"running","session_id":"session-1"}`)
			return
		}
		fmt.Fprint(w, "data: {\"event\":\"run.steered\",\"run_id\":\"run-1\",\"accepted\":true}\n\n")
		fmt.Fprint(w, "data: {\"event\":\"message.delta\",\"run_id\":\"run-1\",\"delta\":\"hello\"}\n\n")
		fmt.Fprint(w, "data: {\"event\":\"run.completed\",\"run_id\":\"run-1\",\"output\":\"hello final\",\"pending_steer\":\"next\\nlast\",\"usage\":{\"input_tokens\":3,\"output_tokens\":2,\"total_tokens\":5}}\n\n")
		fmt.Fprint(w, "data: {\"event\":\"message.delta\",\"run_id\":\"run-1\",\"delta\":\"too late\"}\n\n")
	})
	var events []domain.WSEvent
	result, pending, err := s.readManagedRun(context.Background(), "run-1", "device-1", func(e domain.WSEvent) { events = append(events, e) })
	if err != nil || !result.Terminal || result.Errored || result.FinalText != "hello final" || pending != "next\nlast" || s.GetSessionKey() != "session-1" {
		t.Fatalf("result=%+v pending=%q err=%v", result, pending, err)
	}
	for _, e := range events {
		raw := string(e.Payload)
		if strings.Contains(raw, "too late") || strings.Contains(raw, "run.steered") {
			t.Fatalf("unexpected %s", raw)
		}
		if strings.Contains(raw, `"runId":"run-1"`) {
			t.Fatalf("server ID leaked as device owner %s", raw)
		}
	}
}

func TestManagedRunRecoversOnlyTerminalStatusAfterStreamLoss(t *testing.T) {
	for _, terminal := range []bool{true, false} {
		t.Run(fmt.Sprint(terminal), func(t *testing.T) {
			polls := 0
			stopped := false
			s := managedTestService(t, func(w http.ResponseWriter, r *http.Request) {
				if strings.HasSuffix(r.URL.Path, "/events") {
					return
				}
				if strings.HasSuffix(r.URL.Path, "/stop") {
					stopped = true
					fmt.Fprint(w, `{"status":"stopping"}`)
					return
				}
				polls++
				if polls > 1 && terminal {
					fmt.Fprint(w, `{"run_id":"run-1","status":"completed","output":"recovered","pending_steer":"next"}`)
				} else if stopped {
					fmt.Fprint(w, `{"run_id":"run-1","status":"cancelled"}`)
				} else {
					fmt.Fprint(w, `{"run_id":"run-1","status":"running","session_id":"s"}`)
				}
			})
			result, pending, err := s.readManagedRun(context.Background(), "run-1", "d", func(domain.WSEvent) {})
			if terminal {
				if err != nil || !result.Terminal || result.FinalText != "recovered" || pending != "next" {
					t.Fatalf("%+v %q %v", result, pending, err)
				}
			} else if err != nil || !result.Terminal || !result.Errored || !stopped {
				t.Fatalf("stop did not settle failed ownership: %+v %v", result, err)
			}
		})
	}
}

func TestManagedRunImageUsesCanonicalContentAndSameSession(t *testing.T) {
	imageURL := "data:image/jpeg;base64,aGVsbG8="
	s := managedTestService(t, func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if body["session_id"] != "existing-session" {
			t.Fatalf("session lost: %#v", body)
		}
		input := body["input"].([]any)[0].(map[string]any)
		parts := input["content"].([]any)
		if parts[0].(map[string]any)["type"] != "text" || parts[0].(map[string]any)["text"] != "look here" {
			t.Fatalf("text lost: %#v", parts)
		}
		img := parts[1].(map[string]any)
		if img["type"] != "image_url" || img["image_url"].(map[string]any)["url"] != imageURL {
			t.Fatalf("image lost: %#v", img)
		}
		w.WriteHeader(202)
		fmt.Fprint(w, `{"run_id":"vision-run","status":"started"}`)
	})
	input := []inputMessage{{Role: "user", Content: []inputContent{{Type: "input_text", Text: "look here"}, {Type: "input_image", ImageURL: imageURL}}}}
	id, err := s.createManagedRun(context.Background(), streamRequest{Input: input}, "existing-session")
	if err != nil || id != "vision-run" {
		t.Fatalf("%q %v", id, err)
	}
}

func TestManagedRunRequiresEnrichedCapability(t *testing.T) {
	s := managedTestService(t, func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"features":{"run_submission":true,"run_events_sse":true,"run_status":true,"run_stop":true,"run_steer":true,"runs_idempotency":{"supported":true}}}`)
	})
	if s.discoverRunSteering(context.Background()) {
		t.Fatal("unenriched server changed transport")
	}
}

func TestManagedRunEnrichedToolsAndCache(t *testing.T) {
	s := managedTestService(t, func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasSuffix(r.URL.Path, "/events") {
			fmt.Fprint(w, `{"run_id":"run-1","status":"running","session_id":"s"}`)
			return
		}
		for _, event := range []string{
			`{"event":"tool.started","tool":"terminal","preview":"legacy"}`,
			`{"event":"tool.call.started","tool":"terminal","tool_call_id":"real-call-1","arguments":{"command":"echo hello"}}`,
			`{"event":"tool.completed","tool":"terminal","error":false}`,
			`{"event":"tool.call.completed","tool":"terminal","tool_call_id":"real-call-1","result":{"output":"actual result"}}`,
			`{"event":"run.completed","output":"done","usage":{"input_tokens":100,"output_tokens":4,"total_tokens":104,"cache_read_tokens":60,"cache_write_tokens":20}}`,
		} {
			fmt.Fprintf(w, "data: %s\n\n", event)
		}
	})
	starts, ends := 0, 0
	foundCache := false
	result, _, err := s.readManagedRun(context.Background(), "run-1", "device-1", func(event domain.WSEvent) {
		var payload struct {
			Stream string `json:"stream"`
			Data   struct {
				Phase      string          `json:"phase"`
				ToolCallID string          `json:"toolCallId"`
				Arguments  string          `json:"arguments"`
				Result     json.RawMessage `json:"result"`
			} `json:"data"`
		}
		_ = json.Unmarshal(event.Payload, &payload)
		if payload.Stream == "tool" && payload.Data.Phase == "start" {
			starts++
			if payload.Data.ToolCallID != "real-call-1" || !strings.Contains(payload.Data.Arguments, "echo hello") {
				t.Errorf("lost call: %s", event.Payload)
			}
		}
		if payload.Stream == "tool" && payload.Data.Phase == "end" {
			ends++
			if payload.Data.ToolCallID != "real-call-1" || !strings.Contains(string(payload.Data.Result), "actual result") {
				t.Errorf("lost result: %s", event.Payload)
			}
		}
		if strings.Contains(string(event.Payload), `"cacheReadTokens":60`) && strings.Contains(string(event.Payload), `"cacheWriteTokens":20`) {
			foundCache = true
		}
	})
	if err != nil || !result.Terminal || starts != 1 || ends != 1 || !foundCache {
		t.Fatalf("terminal=%v starts=%d ends=%d cache=%v err=%v", result.Terminal, starts, ends, foundCache, err)
	}
}

func TestManagedRunInitialObserverFailureSettlesRemote(t *testing.T) {
	stopped := false
	s := managedTestService(t, func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/stop") {
			stopped = true
			fmt.Fprint(w, `{"status":"stopping"}`)
			return
		}
		if !stopped {
			w.WriteHeader(503)
			return
		}
		fmt.Fprint(w, `{"run_id":"run-1","status":"cancelled"}`)
	})
	result, _, err := s.readManagedRun(context.Background(), "run-1", "device-1", func(domain.WSEvent) {})
	if err != nil || !result.Terminal || !result.Errored || !stopped {
		t.Fatalf("remote not settled: %+v %v", result, err)
	}
}
