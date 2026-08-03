package i18n

import "testing"

func TestBuildChitchatWakeWordsPreservesLocalAttentionAliases(t *testing.T) {
	words := BuildChitchatWakeWords("Luna")
	want := []string{"hello luna", "hey luna", "này luna", "ê luna", "luna ơi", "luna"}
	if len(words) != len(want) {
		t.Fatalf("BuildChitchatWakeWords() = %v, want %v", words, want)
	}
	for i, word := range want {
		if words[i] != word {
			t.Fatalf("BuildChitchatWakeWords() = %v, want %v", words, want)
		}
	}
}

func TestBuildVoiceWakeWordsUsesHALPrefixes(t *testing.T) {
	words := BuildVoiceWakeWords("Luna")
	want := []string{"wake up luna", "hello luna", "okay luna", "hey luna", "hi luna", "alo luna", "ok luna"}
	if len(words) != len(want) {
		t.Fatalf("BuildVoiceWakeWords() = %v, want %v", words, want)
	}
	for i, word := range want {
		if words[i] != word {
			t.Fatalf("BuildVoiceWakeWords() = %v, want %v", words, want)
		}
	}
}
