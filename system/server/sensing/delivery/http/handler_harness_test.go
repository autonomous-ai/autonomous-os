package http

import (
	"strings"
	"testing"
)

func TestTruncateHarnessFollowupContextPreservesRuneBoundaries(t *testing.T) {
	text := strings.Repeat("é", maxHarnessFollowupContextRunes+1)
	got := truncateHarnessFollowupContext(text)
	if !strings.HasSuffix(got, "…") {
		t.Fatalf("truncated context = %q", got[len(got)-10:])
	}
	if len([]rune(got)) != maxHarnessFollowupContextRunes+1 {
		t.Fatalf("rune length = %d", len([]rune(got)))
	}
}
