package http

import (
	"testing"

	"go.autonomous.ai/os/system/domain"
)

type taskCorrelationGateway struct {
	domain.AgentGateway
	match string
}

func (g *taskCorrelationGateway) MatchPendingTaskByMessage(string) string { return g.match }

func TestTaskCorrelationDoesNotChangeRouting(t *testing.T) {
	for _, match := range []string{"device-chat-queued", ""} {
		t.Run(match, func(t *testing.T) {
			h := &AgentHandler{
				agentGateway: &taskCorrelationGateway{match: match},
				runIDMap:     map[string]string{"uuid": "routing-choice"},
			}
			h.correlateTaskRun("uuid", "message")
			if got := h.resolveRunID("uuid"); got != "routing-choice" {
				t.Fatalf("telemetry changed runtime routing: %q", got)
			}
			want := match
			if want == "" {
				want = "uuid"
			}
			if got := h.resolveTaskRunID("uuid", "routing-choice"); got != want {
				t.Fatalf("task evidence mapped to %q, want %q", got, want)
			}
		})
	}
}

func TestTaskCorrelationUnsupportedRuntimeUsesExistingRun(t *testing.T) {
	h := &AgentHandler{}
	h.correlateTaskRun("uuid", "message")
	if got := h.resolveTaskRunID("uuid", "device-chat-normal"); got != "device-chat-normal" {
		t.Fatalf("changed runtime without optional matcher: %q", got)
	}
}

func TestTaskCorrelationDuplicateStartKeepsAliasAndEvictionDoesNotGuess(t *testing.T) {
	gateway := &taskCorrelationGateway{match: "device-chat-queued"}
	h := &AgentHandler{agentGateway: gateway}
	h.correlateTaskRun("uuid", "message")
	gateway.match = ""
	h.correlateTaskRun("uuid", "message")
	if got := h.resolveTaskRunID("uuid", "routing-guess"); got != "device-chat-queued" {
		t.Fatalf("duplicate start lost alias: %q", got)
	}
	delete(h.taskRunIDs, "uuid")
	if got := h.resolveTaskRunID("uuid", "routing-guess"); got != "uuid" {
		t.Fatalf("missing alias promoted routing guess: %q", got)
	}
}
