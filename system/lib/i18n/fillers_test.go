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
