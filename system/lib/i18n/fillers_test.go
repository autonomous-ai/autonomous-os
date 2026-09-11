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

// The range demo narrates each leg through these pools. A pool that resolves
// empty is not an error anywhere in the chain — PlayPoolFillerNow returns at
// len == 0 and the HTTP call still answers 200 — so the demo would simply move
// in silence, which is the canned animation it replaces.
func TestDemoPoolsExistInEveryLanguage(t *testing.T) {
	for _, lang := range []string{LangEN, LangVI, LangZhCN, LangZhTW} {
		for _, pool := range []string{
			"demo_intro", "demo_left", "demo_right",
			"demo_up", "demo_down", "demo_done",
		} {
			if got := FillerForTool(lang, pool); len(got) == 0 {
				t.Errorf("pool %q is empty for lang %q", pool, lang)
			}
		}
	}
}

// A key missing from a KNOWN language used to resolve to nothing: the lang map
// existed, so the unknown-lang fallback never fired, and the miss was silent.
// zh had no look_* entries at all, so every look filler on a Chinese device was
// dropped without a log line.
//
// The key is INJECTED because every pool now exists in every language, so there
// is no naturally-missing one left to test with — and a version of this test
// written against a key that turned out to be present passed while the fallback
// was not there at all.
func TestAMissingKeyFallsBackToEnglishRatherThanSilence(t *testing.T) {
	const injected = "zz_test_only_in_english"
	toolFillers[LangEN][injected] = []string{"English only"}
	t.Cleanup(func() { delete(toolFillers[LangEN], injected) })

	for _, lang := range []string{LangVI, LangZhCN, LangZhTW} {
		got := FillerForTool(lang, injected)
		if len(got) == 0 {
			t.Errorf("%s: a known language with a missing key went silent instead of falling back", lang)
			continue
		}
		if got[0] != "English only" {
			t.Errorf("%s: fell back to %q, want the English pool", lang, got[0])
		}
	}
}

// Every pool currently carries its own copy in every language, so the fallback
// above should never actually fire in production. Pinned: a key added to one
// language and forgotten in the others would otherwise start speaking English
// on those devices, and the fallback would hide it rather than surface it.
func TestEveryPoolIsTranslatedInEveryLanguage(t *testing.T) {
	for _, lang := range []string{LangEN, LangVI, LangZhCN, LangZhTW} {
		for _, k := range AllPoolKeys() {
			if len(toolFillers[lang][k]) == 0 {
				t.Errorf("pool %q has no %s translation — it will speak English there", k, lang)
			}
		}
	}
}

// The fallback must not turn "no such pool" into a pool. Callers test for an
// empty result to decide whether to use the generic continuation instead.
func TestAnUnknownPoolStillResolvesToNothing(t *testing.T) {
	if got := FillerForTool(LangEN, "no_such_pool_anywhere"); len(got) != 0 {
		t.Errorf("unknown pool resolved to %v, want nothing", got)
	}
	if got := FillerForTool(LangVI, ""); got != nil {
		t.Errorf("empty tool name resolved to %v, want nil", got)
	}
}

func TestAllPoolKeysCoversEveryPool(t *testing.T) {
	seen := map[string]bool{}
	for _, k := range AllPoolKeys() {
		seen[k] = true
	}
	for _, want := range []string{
		"look_searching", "look_still_searching", "look_found", "look_lost",
		"look_capturing", "demo_intro", "demo_left", "demo_done", "web_search",
	} {
		if !seen[want] {
			t.Errorf("AllPoolKeys() is missing %q — its phrases would never be prewarmed", want)
		}
	}
}

// FillerForTool normalises its argument before lookup, and the normaliser is
// aggressive: HasSuffix(key, "_search") rewrites to "web_search", and there are
// similar rules for _fetch, read, patch and the generate family. A pool key
// that is not its own normalised form would be UNREACHABLE through the public
// lookup — and the prewarm, which enumerates these keys, would silently render
// some other pool's phrases twice and this one's never.
func TestEveryPoolKeyIsItsOwnNormalisedForm(t *testing.T) {
	for _, k := range AllPoolKeys() {
		if got := FillerToolKey(k); got != k {
			t.Errorf("pool key %q normalises to %q — it can never be looked up", k, got)
		}
	}
}
