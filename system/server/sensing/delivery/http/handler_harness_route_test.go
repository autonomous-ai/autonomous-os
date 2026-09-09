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
		{"What is the weather tomorrow?", false},
		{"Open Chrome on my Mac.", false},
		{"Ask Autonomous Buddy to open Chrome.", true},
		{"Tell Buddy to check my calendar.", true},
	}
	for _, test := range tests {
		if got := harnessAgentRequest.MatchString(test.message); got != test.want {
			t.Errorf("MatchString(%q) = %v, want %v", test.message, got, test.want)
		}
	}
}

func TestHarnessRoutingTakesPriorityForExplicitBuddyRequests(t *testing.T) {
	for _, message := range []string{
		"Ask Autonomous Buddy to open Chrome.",
		"Tell Buddy to check my calendar.",
	} {
		if !harnessAgentRequest.MatchString(message) || !buddyAgentRequest.MatchString(message) {
			t.Fatalf("explicit Buddy request should preserve both routing signals: %q", message)
		}
	}
}

func TestHarnessNamedAgentRoutingTreatsNameAsTarget(t *testing.T) {
	for _, required := range []string{
		"execution target, not a person to contact",
		"Ask David if there are events in the US",
		"Find upcoming events in the US",
		"make no more Harness or shell calls",
		"DeliveryUnknown/no usable receipt",
	} {
		if !strings.Contains(harnessNamedAgentRouting, required) {
			t.Fatalf("named-agent routing is missing %q", required)
		}
	}
}
