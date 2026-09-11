package http

import (
	"encoding/json"
	"strings"
	"testing"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/monitor"
	sensinghttp "go.autonomous.ai/os/system/server/sensing/delivery/http"
)

type fillerEventGateway struct{ domain.AgentGateway }

func (fillerEventGateway) GetSessionKey() string    { return "test-session" }
func (fillerEventGateway) IsWebChatRun(string) bool { return false }
func (fillerEventGateway) IsSilentRun(string) bool  { return false }

func TestAssistantEventKeepsFillersEligibleForLaterHermesTools(t *testing.T) {
	id := "device-chat-filler-events"
	fm := sensinghttp.DefaultFillerManager
	fm.MarkVoiceRun(id, "")
	fm.OnTurnStart(id)
	t.Cleanup(func() { fm.Cancel(id) })
	h := &AgentHandler{
		agentGateway: fillerEventGateway{},
		monitorBus:   monitor.ProvideBus(),
		assistantBuf: make(map[string]*strings.Builder),
		streamStats:  make(map[string]*runStreamStats),
	}
	for _, event := range []map[string]any{
		{"stream": "assistant", "data": map[string]any{"delta": "Let me check"}},
		{"stream": "tool", "data": map[string]any{"phase": "start", "name": "terminal", "toolCallId": "terminal-1", "arguments": "{}"}},
		{"stream": "tool", "data": map[string]any{"phase": "end", "toolCallId": "terminal-1"}},
	} {
		event["runId"] = id
		payload, err := json.Marshal(event)
		if err != nil {
			t.Fatal(err)
		}
		if err := h.handleAgentStreamEvent(domain.WSEvent{Payload: payload}); err != nil {
			t.Fatal(err)
		}
		if !fm.HasActiveRun(id) {
			t.Fatalf("%s event permanently removed a voice run before real speech", event["stream"])
		}
	}
}
