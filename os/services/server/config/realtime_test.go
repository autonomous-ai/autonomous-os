package config

import (
	"encoding/json"
	"strings"
	"testing"
)

func boolPtr(b bool) *bool { return &b }

// With no realtime block the accessors return HAL/Python-mirroring defaults —
// enabled + provider gemini, flash model, Kore voice — and the reasoning knob
// defaults to the cost-lean MINIMAL (deliberately below HAL's HIGH). Keys/URLs
// fall back to the LLM credentials.
func TestRealtime_DefaultsWhenUnset(t *testing.T) {
	c := &Config{LLMAPIKey: "llm-key", LLMBaseURL: "https://llm.example"}
	if !c.RealtimeEnabled() {
		t.Error("RealtimeEnabled() = false, want true by default")
	}
	if got := c.RealtimeProvider(); got != "gemini" {
		t.Errorf("RealtimeProvider() = %q, want gemini default", got)
	}
	if got := c.RealtimeModel(); got != defaultRealtimeGeminiModel {
		t.Errorf("RealtimeModel() = %q, want %q", got, defaultRealtimeGeminiModel)
	}
	if got := c.RealtimeVoice(); got != defaultRealtimeGeminiVoice {
		t.Errorf("RealtimeVoice() = %q, want %q", got, defaultRealtimeGeminiVoice)
	}
	if got := c.RealtimeReasoning(); got != "MINIMAL" {
		t.Errorf("RealtimeReasoning() = %q, want MINIMAL (cost-lean default)", got)
	}
	if got := c.RealtimeAPIKey(); got != "llm-key" {
		t.Errorf("RealtimeAPIKey() = %q, want LLM fallback", got)
	}
	if got := c.RealtimeBaseURL(); got != "https://llm.example" {
		t.Errorf("RealtimeBaseURL() = %q, want LLM fallback", got)
	}
}

// Enabled defaults true; only an explicit false (or provider none) turns it off.
func TestRealtime_EnabledAndOff(t *testing.T) {
	if (&Config{Realtime: &RealtimeConfig{}}).RealtimeEnabled() != true {
		t.Error("empty block → want enabled true")
	}
	off := &Config{Realtime: &RealtimeConfig{Enabled: boolPtr(false)}}
	if off.RealtimeEnabled() {
		t.Error("Enabled:false → want false")
	}
	// provider none disables via the provider path; model/voice go empty (off).
	none := &Config{Realtime: &RealtimeConfig{Provider: "none"}}
	if none.RealtimeProvider() != "" {
		t.Errorf("provider none → want \"\", got %q", none.RealtimeProvider())
	}
	if none.RealtimeModel() != "" || none.RealtimeReasoning() != "" {
		t.Error("provider none → model/reasoning should be empty (off)")
	}
}

// provider normalizes: empty/unset → gemini default; none/off/disabled → "".
func TestRealtime_ProviderNormalize(t *testing.T) {
	cases := map[string]string{
		"  Gemini ": "gemini", "OPENAI": "openai",
		"": "gemini", "none": "", "off": "", "disabled": "",
	}
	for in, want := range cases {
		c := &Config{Realtime: &RealtimeConfig{Provider: in}}
		if got := c.RealtimeProvider(); got != want {
			t.Errorf("RealtimeProvider(%q) = %q, want %q", in, got, want)
		}
	}
}

// The active provider selects which sub-object the knobs read; explicit overrides
// beat the defaults, and the inactive provider's block is ignored.
func TestRealtime_ProviderAwareOverrides(t *testing.T) {
	c := &Config{Realtime: &RealtimeConfig{
		Provider: "gemini",
		Gemini:   &GeminiRealtime{Model: "gem-live", Voice: "Charon", ThinkingLevel: "HIGH"},
		OpenAI:   &OpenAIRealtime{Model: "gpt-rt", Voice: "echo", ReasoningEffort: "high"},
	}}
	if c.RealtimeModel() != "gem-live" || c.RealtimeVoice() != "Charon" || c.RealtimeReasoning() != "HIGH" {
		t.Errorf("gemini overrides not applied: model=%q voice=%q reasoning=%q",
			c.RealtimeModel(), c.RealtimeVoice(), c.RealtimeReasoning())
	}
	c.Realtime.Provider = "openai"
	if c.RealtimeModel() != "gpt-rt" || c.RealtimeReasoning() != "high" {
		t.Errorf("openai overrides not applied after switch: model=%q reasoning=%q",
			c.RealtimeModel(), c.RealtimeReasoning())
	}
}

// Active provider with no sub-object → provider defaults (not empty); per-field
// key/baseURL override beats the LLM fallback.
func TestRealtime_MissingSubAndKeyOverride(t *testing.T) {
	c := &Config{
		LLMAPIKey:  "llm-key",
		LLMBaseURL: "https://llm.example",
		Realtime:   &RealtimeConfig{Provider: "openai"}, // no OpenAI sub
	}
	if got := c.RealtimeModel(); got != defaultRealtimeOpenAIModel {
		t.Errorf("RealtimeModel() = %q, want openai default %q", got, defaultRealtimeOpenAIModel)
	}
	if got := c.RealtimeReasoning(); got != "minimal" {
		t.Errorf("RealtimeReasoning() = %q, want openai cost-lean default minimal", got)
	}
	if got := c.RealtimeAPIKey(); got != "llm-key" {
		t.Errorf("RealtimeAPIKey() = %q, want LLM fallback", got)
	}
	c.Realtime.APIKey = "rt-key"
	c.Realtime.BaseURL = "https://rt.example"
	if c.RealtimeAPIKey() != "rt-key" || c.RealtimeBaseURL() != "https://rt.example" {
		t.Errorf("override not applied: key=%q url=%q", c.RealtimeAPIKey(), c.RealtimeBaseURL())
	}
}

// The pointer field must omit cleanly: a nil Realtime emits no "realtime" key,
// while a present block round-trips. (Guards the omitempty-on-struct gotcha — a
// value field would always marshal "realtime":{}.)
func TestRealtime_JSONOmitAndRoundTrip(t *testing.T) {
	noBlock, err := json.Marshal(&Config{LLMAPIKey: "k"})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if strings.Contains(string(noBlock), "realtime") {
		t.Errorf("nil Realtime should omit the key, got: %s", noBlock)
	}

	in := &Config{Realtime: &RealtimeConfig{
		Provider: "openai",
		Enabled:  boolPtr(true),
		OpenAI:   &OpenAIRealtime{Model: "gpt-rt", Voice: "alloy", ReasoningEffort: "xhigh"},
	}}
	data, err := json.Marshal(in)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var out Config
	if err := json.Unmarshal(data, &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if out.RealtimeProvider() != "openai" || out.RealtimeModel() != "gpt-rt" || out.RealtimeReasoning() != "xhigh" {
		t.Errorf("round-trip lost data: provider=%q model=%q reasoning=%q",
			out.RealtimeProvider(), out.RealtimeModel(), out.RealtimeReasoning())
	}
}
