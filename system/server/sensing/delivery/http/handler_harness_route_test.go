package http

import (
	"strings"
	"testing"
)

func TestHarnessAgentRequest(t *testing.T) {
	tests := []struct {
		message string
		want    bool
	}{
		{"Ask agent David if anything is happening tomorrow.", true},
		{"Tell the Harness agent to review this code.", true},
		{"Nhờ agent David kiểm tra lại giúp tôi.", true},
		{"y minh la hoi mike agent la BTC price the nao", true},
		{"nho agent Mike tra gia BTC", true},
		{"hoi thoi tiet ngay mai", false},
		{"What is the weather tomorrow?", false},
		{"Open Chrome on my Mac.", false},
		{"Ask Autonomous Buddy to open Chrome.", false},
		{"Tell Buddy to check my calendar.", false},
	}
	for _, test := range tests {
		if got := harnessAgentRequest.MatchString(test.message); got != test.want {
			t.Errorf("MatchString(%q) = %v, want %v", test.message, got, test.want)
		}
	}
}

func TestHarnessRequestRouting(t *testing.T) {
	tests := []struct {
		message  string
		followup bool
		want     string
	}{
		{"Check my calendar tomorrow", false, ""},
		{"Have a nice day", false, ""},
		{"Tell me a joke", false, ""},
		{"Nhờ kiểm tra thời tiết", false, ""},
		{"hoi thoi tiet ngay mai", false, ""},
		{"Ask Mike to review this repository", false, harnessAgentDiscoveryRouting},
		{"Ask Mike to review this repository", true, harnessAgentDiscoveryRouting},
		{"Nhờ David review code", false, harnessAgentDiscoveryRouting},
		{"Ask agent Mike to review this repository", true, harnessNamedAgentRouting},
		{"Hỏi agent Minh xem giá BTC", false, harnessNamedAgentRouting},
		{"nho agent Mike tra gia BTC", false, harnessNamedAgentRouting},
		{"hoi mike agent la BTC price the nao", false, harnessNamedAgentRouting},
		{"Use Harness to research this topic", true, harnessNamedAgentRouting},
		{"Ask a research agent to investigate this topic", true, harnessNamedAgentRouting},
		{"Have a Harness agent fix reconnect in autonomous", true, harnessNamedAgentRouting},
		{"Ask a coding agent to review this repository", false, harnessNamedAgentRouting},
		{"Nhờ một agent kiểm tra code", true, harnessNamedAgentRouting},
		{"nho mot agent kiem tra code", false, harnessNamedAgentRouting},
		{"Ask the Harness agent to review this code", false, harnessNamedAgentRouting},
		{"Is it done yet?", true, harnessFollowupRouting},
		{"Ask Autonomous Buddy to open Chrome", true, ""},
		{"Tell Buddy to check my calendar", true, ""},
		{"Nhờ Buddy mở Chrome", true, ""},
	}
	for _, tt := range tests {
		t.Run(tt.message, func(t *testing.T) {
			if got := harnessRequestRouting(tt.message, tt.followup); got != tt.want {
				t.Errorf("routing = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestHarnessNamedAgentRoutingTreatsNameAsTarget(t *testing.T) {
	for _, required := range []string{
		"execution target, not a person to contact",
		"An explicitly requested agent takes priority over any retained target",
		"For a new task without an explicit name, follow the skill task-based selection policy",
		"Ask David if there are events in the US",
		"Find upcoming events in the US",
		"make no more Harness or shell calls",
		"never return NO_REPLY until that new send/answer has a known receipt",
		"DeliveryUnknown/no usable receipt",
	} {
		if !strings.Contains(harnessNamedAgentRouting, required) {
			t.Fatalf("named-agent routing is missing %q", required)
		}
	}
}

func TestHarnessFollowupRoutingSupportsChatCompletionChecks(t *testing.T) {
	const instruction = "ask whether it has finished or for its result"
	if !strings.Contains(harnessFollowupRouting, instruction) {
		t.Fatalf("Harness follow-up routing is missing completion checks")
	}
}
