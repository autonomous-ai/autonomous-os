package http

import "testing"

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
	}
	for _, test := range tests {
		if got := harnessAgentRequest.MatchString(test.message); got != test.want {
			t.Errorf("MatchString(%q) = %v, want %v", test.message, got, test.want)
		}
	}
}
