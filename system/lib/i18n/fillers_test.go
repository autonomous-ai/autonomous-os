package i18n

import "testing"

func TestFillerToolKeyNormalisesRuntimeNames(t *testing.T) {
	tests := map[string]string{
		"web_search":                     "web_search",  // OpenClaw / Codex
		"bash":                           "exec",        // OpenCode
		"shell":                          "exec",        // Codex
		"command_execution":              "exec",        // Codex item kind
		"file_changes":                   "apply_patch", // Codex item kind
		"mcp__filesystem__read":          "read",
		"mcp__browser__web_search":       "web_search",
		"mcp__runner__shell":             "exec",
		"memory_search":                  "memory_search",
		"unrecognised_runtime_tool_name": "unrecognised_runtime_tool_name",
	}
	for raw, want := range tests {
		if got := FillerToolKey(raw); got != want {
			t.Errorf("FillerToolKey(%q) = %q, want %q", raw, got, want)
		}
	}
}

func TestFillerRealtimeUsesDedicatedVietnamesePool(t *testing.T) {
	got := FillerRealtime(LangVI)
	want := map[string]bool{"Ừm...": true, "Hừm...": true}
	if len(got) != len(want) {
		t.Fatalf("FillerRealtime(%q) = %v, want %d phrases", LangVI, got, len(want))
	}
	for _, phrase := range got {
		if !want[phrase] {
			t.Errorf("unexpected realtime filler %q", phrase)
		}
	}
}

func TestFillerContinuationUsesNaturalVietnameseThoughtSounds(t *testing.T) {
	got := FillerContinuation(LangVI)
	want := map[string]bool{
		"Ừm, để coi.":      true,
		"Ờ, chờ tí.":       true,
		"Hừm, để thử xem.": true,
		"À, để mình ngó.":  true,
		"Ừ, để xem nào.":   true,
	}
	if len(got) != len(want) {
		t.Fatalf("FillerContinuation(%q) = %v, want %d phrases", LangVI, got, len(want))
	}
	for _, phrase := range got {
		if !want[phrase] {
			t.Errorf("unexpected continuation filler %q", phrase)
		}
	}
}
