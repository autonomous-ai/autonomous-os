package http

import (
	"os"
	"strings"
	"testing"

	"go.autonomous.ai/os/system/lib/i18n"
)

// PrewarmFillers must enumerate pools from the pool maps, not from a list
// maintained by hand beside it.
//
// A pool that is not prewarmed renders live at the TTS provider on its first
// fire (~1-2s) and the late audio races the speech after it — the comment above
// that loop says so itself. The hand-maintained list had already fallen behind
// twice: every look_* pool was missing, and the demo_* pools would have been
// missing too.
//
// Asserted against the source because the function itself talks to the TTS
// provider and cannot be run here. That makes this a weak test of a strong
// invariant — it catches the list drifting back, not a subtler mistake.
func TestPrewarmEnumeratesPoolsFromTheMaps(t *testing.T) {
	src, err := os.ReadFile("deadair_filler.go")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(src), "i18n.AllPoolKeys()") {
		t.Error("PrewarmFillers must enumerate pools via i18n.AllPoolKeys()")
	}
	for _, gone := range []string{`"web_search", "x_search"`, `"pdf", "canvas"`} {
		if strings.Contains(string(src), gone) {
			t.Errorf("a hand-maintained tool list is back in the prewarm: %s", gone)
		}
	}
}

// Everything the prewarm will render must be reachable through the same lookup
// the runtime uses, in every language it may run in.
func TestEveryPrewarmedPoolResolvesInEveryLanguage(t *testing.T) {
	keys := i18n.AllPoolKeys()
	if len(keys) == 0 {
		t.Fatal("no pool keys to prewarm")
	}
	for _, lang := range []string{i18n.LangEN, i18n.LangVI, i18n.LangZhCN, i18n.LangZhTW} {
		for _, k := range keys {
			if len(i18n.FillerForTool(lang, k)) == 0 {
				t.Errorf("pool %q resolves to nothing in %q — it would be prewarmed as silence", k, lang)
			}
		}
	}
}
