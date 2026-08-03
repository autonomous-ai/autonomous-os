package openclaw

import "testing"

func TestBuildWakeWordsUsesSharedEnglishAliases(t *testing.T) {
	want := []string{"wake up moon", "hello moon", "okay moon", "hey moon", "hi moon", "alo moon", "ok moon"}
	words := buildWakeWords("Moon")
	if len(words) != len(want) {
		t.Fatalf("buildWakeWords() = %v, want %v", words, want)
	}
	for i, word := range want {
		if words[i] != word {
			t.Fatalf("buildWakeWords() = %v, want %v", words, want)
		}
	}
}
