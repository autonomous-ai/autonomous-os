package i18n

import "testing"

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
