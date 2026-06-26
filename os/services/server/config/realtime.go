package config

import (
	"fmt"
	"strings"
)

// This file holds the realtime voice-agent (audio-native brain — Gemini Live /
// OpenAI Realtime) config types, defaults, and accessors. The Config.Realtime
// field that hangs these off the main config lives in config.go.

// RealtimeConfig groups the realtime voice-agent settings under the "realtime"
// key. Shared fields (enabled/provider/api_key/base_url) sit at the top; the
// per-provider knobs live in Gemini/OpenAI sub-objects, mirroring HAL's own
// GeminiConfig/OpenAIConfig dataclasses and the orchestrator's provider factory.
// `provider` selects which sub-object is active; both are kept so switching
// provider in the UI does not lose the other's tuned model/voice/reasoning.
//
// Defaults mirror HAL/Python (os/hal/config.py): unset → enabled + provider
// "gemini", so realtime runs out of the box. Only an explicit enabled:false or
// provider:"none" turns it off. Fields NOT modelled here (turn detection, session
// resumption, sample rate, memory/summarizer) stay governed by HAL's env/defaults
// — only the operator-facing knobs are lifted, exactly as the TTS config lifts
// provider/voice/instructions but not the VAD internals.
type RealtimeConfig struct {
	// Enabled toggles the realtime brain. Unset → true (mirrors HAL's
	// HAL_REALTIME_ENABLED default); set false to disable.
	Enabled  *bool           `json:"enabled,omitempty" yaml:"enabled"`
	Provider string          `json:"provider,omitempty" yaml:"provider"` // none|gemini|openai ("" == none)
	APIKey   string          `json:"api_key,omitempty" yaml:"apiKey"`    // empty → falls back to LLMAPIKey
	BaseURL  string          `json:"base_url,omitempty" yaml:"baseURL"`  // empty → falls back to LLMBaseURL
	Gemini   *GeminiRealtime `json:"gemini,omitempty" yaml:"gemini"`
	OpenAI   *OpenAIRealtime `json:"openai,omitempty" yaml:"openai"`
}

// GeminiRealtime holds Gemini Live's provider-specific knobs. Empty fields → HAL
// applies its own default (e.g. model "gemini-…-live-preview", voice "Kore").
type GeminiRealtime struct {
	Model         string `json:"model,omitempty" yaml:"model"`
	Voice         string `json:"voice,omitempty" yaml:"voice"`                  // Gemini voice set (e.g. Kore)
	ThinkingLevel string `json:"thinking_level,omitempty" yaml:"thinkingLevel"` // Gemini-only reasoning knob (e.g. HIGH)
	// GoogleSearch toggles Google Search grounding (Gemini-only). nil → HAL default
	// (on). Kept here so an operator's explicit override survives config re-saves
	// instead of being silently dropped on the next marshal.
	GoogleSearch *bool `json:"google_search,omitempty" yaml:"googleSearch"`
}

// OpenAIRealtime holds OpenAI Realtime's provider-specific knobs. Empty fields →
// HAL applies its own default (e.g. model "gpt-realtime-…", voice "alloy").
type OpenAIRealtime struct {
	Model           string `json:"model,omitempty" yaml:"model"`
	Voice           string `json:"voice,omitempty" yaml:"voice"`                      // OpenAI voice set (e.g. alloy)
	ReasoningEffort string `json:"reasoning_effort,omitempty" yaml:"reasoningEffort"` // OpenAI-only reasoning knob (e.g. xhigh)
}

// Realtime per-provider defaults — what os-server resolves (and pushes) when the
// operator hasn't overridden a knob. Model/voice match HAL's defaults
// (os/hal/config.py): the Gemini model is the flash (cheapest) live variant. The
// reasoning knobs DELIBERATELY DIVERGE from HAL toward the CHEAPEST tier (Gemini
// MINIMAL, OpenAI minimal) instead of HAL's HIGH/xhigh — os-server picks a
// cost-lean default; an operator who wants deeper reasoning sets it explicitly.
// Values must stay valid against HAL's enums (GeminiThinkingLevel / GeminiVoice /
// OpenAIReasoningEffort / OpenAI voices).
const (
	// 2.5 native-audio: same per-turn token usage as 3.1 but ~33% cheaper text
	// tokens. HAL omits speech_config.language_code for native-audio models
	// (they reject it). Override via realtime.gemini.model in config.json.
	defaultRealtimeGeminiModel     = "gemini-2.5-flash-native-audio-preview-12-2025"
	defaultRealtimeGeminiVoice     = "Kore"
	defaultRealtimeGeminiThinking  = "MINIMAL"
	defaultRealtimeOpenAIModel     = "gpt-realtime-2"
	defaultRealtimeOpenAIVoice     = "alloy"
	defaultRealtimeOpenAIReasoning = "minimal"
)

// DefaultRealtimeConfig returns the realtime block os-server seeds into
// config.json on first start (or after an upgrade) when none is present, so the
// file always carries an editable realtime config. Values come from the provider
// defaults above; HAL then reads them straight from config.json. api_key/base_url
// are intentionally left empty so they fall back to the LLM credentials.
func DefaultRealtimeConfig() *RealtimeConfig {
	enabled := true
	return &RealtimeConfig{
		Enabled:  &enabled,
		Provider: "gemini",
		Gemini: &GeminiRealtime{
			Model:         defaultRealtimeGeminiModel,
			Voice:         defaultRealtimeGeminiVoice,
			ThinkingLevel: defaultRealtimeGeminiThinking,
		},
		OpenAI: &OpenAIRealtime{
			Model:           defaultRealtimeOpenAIModel,
			Voice:           defaultRealtimeOpenAIVoice,
			ReasoningEffort: defaultRealtimeOpenAIReasoning,
		},
	}
}

// --- Realtime voice-agent accessors -----------------------------------------
// All are nil-safe so callers never touch the nested struct directly; keys/URLs
// fall back to the LLM credentials (same pattern as Get{TTS,STT}*), and the
// model/voice/reasoning getters resolve the ACTIVE provider's sub-object.

// RealtimeEnabled reports whether the realtime brain should run. Defaults to true
// (mirrors HAL's HAL_REALTIME_ENABLED default) — only an explicit enabled:false
// turns it off (so does provider:"none", via RealtimeProvider).
func (c *Config) RealtimeEnabled() bool {
	if c.Realtime != nil && c.Realtime.Enabled != nil {
		return *c.Realtime.Enabled
	}
	return true
}

// RealtimeProvider returns the normalized provider ("gemini"/"openai"), defaulting
// to "gemini" (mirrors HAL's HAL_REALTIME_PROVIDER default). An explicit
// none/off/disabled returns "" — realtime off. Matches the HAL orchestrator's
// provider vocabulary so the value can be pushed through verbatim.
func (c *Config) RealtimeProvider() string {
	p := ""
	if c.Realtime != nil {
		p = strings.ToLower(strings.TrimSpace(c.Realtime.Provider))
	}
	switch p {
	case "none", "off", "disabled":
		return ""
	case "":
		return "gemini"
	default:
		return p
	}
}

// RealtimeAPIKey returns the realtime provider key, falling back to LLMAPIKey.
func (c *Config) RealtimeAPIKey() string {
	if c.Realtime != nil && c.Realtime.APIKey != "" {
		return c.Realtime.APIKey
	}
	return c.LLMAPIKey
}

// RealtimeBaseURL returns the realtime provider base URL, falling back to LLMBaseURL.
//
// NOTE: this is the RESOLVED endpoint and is intentionally NOT what the public
// config / web form shows. The bare LLMBaseURL fallback lacks the provider WS
// suffix (HAL appends "/ws/gemini" itself when the override is empty), so echoing
// it into the editable "Base URL (leave blank to derive)" field would make the
// web re-persist a bare URL on the next save — which HAL then hands to the genai
// SDK verbatim, producing a 404 at the Gemini Live handshake. Use
// RealtimeBaseURLOverride for display so the field stays blank when deriving.
func (c *Config) RealtimeBaseURL() string {
	if c.Realtime != nil && c.Realtime.BaseURL != "" {
		return c.Realtime.BaseURL
	}
	return c.LLMBaseURL
}

// RealtimeBaseURLOverride returns ONLY the operator's explicit base_url override
// (empty when unset), without the LLMBaseURL fallback. The public config exposes
// this so the web "Base URL (leave blank to derive)" field reflects whether an
// override is actually set — see RealtimeBaseURL for why the resolved value must
// not leak into the editable form.
func (c *Config) RealtimeBaseURLOverride() string {
	if c.Realtime != nil {
		return c.Realtime.BaseURL
	}
	return ""
}

// RealtimeModel returns the active provider's model — the operator override when
// set, else the provider default (mirrors HAL). "" only when realtime is off.
func (c *Config) RealtimeModel() string {
	switch c.RealtimeProvider() {
	case "gemini":
		if c.Realtime != nil && c.Realtime.Gemini != nil && c.Realtime.Gemini.Model != "" {
			return c.Realtime.Gemini.Model
		}
		return defaultRealtimeGeminiModel
	case "openai":
		if c.Realtime != nil && c.Realtime.OpenAI != nil && c.Realtime.OpenAI.Model != "" {
			return c.Realtime.OpenAI.Model
		}
		return defaultRealtimeOpenAIModel
	}
	return ""
}

// RealtimeVoice returns the active provider's voice — override or provider default.
func (c *Config) RealtimeVoice() string {
	switch c.RealtimeProvider() {
	case "gemini":
		if c.Realtime != nil && c.Realtime.Gemini != nil && c.Realtime.Gemini.Voice != "" {
			return c.Realtime.Gemini.Voice
		}
		return defaultRealtimeGeminiVoice
	case "openai":
		if c.Realtime != nil && c.Realtime.OpenAI != nil && c.Realtime.OpenAI.Voice != "" {
			return c.Realtime.OpenAI.Voice
		}
		return defaultRealtimeOpenAIVoice
	}
	return ""
}

// RealtimeReasoning returns the active provider's reasoning knob — Gemini's
// thinking_level or OpenAI's reasoning_effort — override or the (cost-lean)
// provider default. Empty only when realtime is off.
func (c *Config) RealtimeReasoning() string {
	switch c.RealtimeProvider() {
	case "gemini":
		if c.Realtime != nil && c.Realtime.Gemini != nil && c.Realtime.Gemini.ThinkingLevel != "" {
			return c.Realtime.Gemini.ThinkingLevel
		}
		return defaultRealtimeGeminiThinking
	case "openai":
		if c.Realtime != nil && c.Realtime.OpenAI != nil && c.Realtime.OpenAI.ReasoningEffort != "" {
			return c.Realtime.OpenAI.ReasoningEffort
		}
		return defaultRealtimeOpenAIReasoning
	}
	return ""
}

// --- Validation (for realtime.set MQTT downlinks) ---------------------------
// Valid knob values per provider — KEEP IN SYNC with os/hal/drivers/realtime/
// enums (GeminiVoice / GeminiThinkingLevel / OpenAIReasoningEffort) and the
// OpenAI voice list in os/hal/routes/voice.py. Case-sensitive to match the HAL
// StrEnums (Gemini voices are Capitalized, OpenAI voices/efforts are lowercase).
var (
	realtimeGeminiVoices    = map[string]bool{"Puck": true, "Charon": true, "Kore": true, "Fenrir": true, "Aoede": true}
	realtimeOpenAIVoices    = map[string]bool{"alloy": true, "ash": true, "coral": true, "echo": true, "fable": true, "onyx": true, "nova": true, "sage": true, "shimmer": true}
	realtimeGeminiThinking  = map[string]bool{"MINIMAL": true, "LOW": true, "MEDIUM": true, "HIGH": true}
	realtimeOpenAIReasoning = map[string]bool{"minimal": true, "low": true, "medium": true, "high": true, "xhigh": true}
)

// Ordered option lists — the SINGLE SOURCE the web reads via GET realtime options
// (so the FE never hardcodes/drifts). Order matters: first reasoning entry is the
// cheapest (the default). Voices match the maps below; KEEP IN SYNC with the HAL
// enums (os/hal/drivers/realtime/enums).
var (
	RealtimeProviders           = []string{"gemini", "openai", "none"}
	RealtimeGeminiVoiceList     = []string{"Puck", "Charon", "Kore", "Fenrir", "Aoede"}
	RealtimeOpenAIVoiceList     = []string{"alloy", "ash", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"}
	RealtimeGeminiThinkingList  = []string{"MINIMAL", "LOW", "MEDIUM", "HIGH"}
	RealtimeOpenAIReasoningList = []string{"minimal", "low", "medium", "high", "xhigh"}
)

// RealtimeOptions is the payload for the realtime-options endpoint: valid
// providers, and the per-provider voice/reasoning lists the web renders.
type RealtimeOptions struct {
	Providers []string            `json:"providers"`
	Voices    map[string][]string `json:"voices"`
	Reasoning map[string][]string `json:"reasoning"`
}

// GetRealtimeOptions returns the valid realtime option lists.
func GetRealtimeOptions() RealtimeOptions {
	return RealtimeOptions{
		Providers: RealtimeProviders,
		Voices:    map[string][]string{"gemini": RealtimeGeminiVoiceList, "openai": RealtimeOpenAIVoiceList},
		Reasoning: map[string][]string{"gemini": RealtimeGeminiThinkingList, "openai": RealtimeOpenAIReasoningList},
	}
}

// ValidateRealtimeProvider accepts the provider selector (gemini|openai|none and
// the off-synonyms / empty). Anything else is rejected.
func ValidateRealtimeProvider(provider string) error {
	switch strings.ToLower(strings.TrimSpace(provider)) {
	case "gemini", "openai", "none", "off", "disabled", "":
		return nil
	default:
		return fmt.Errorf("invalid realtime provider %q (want gemini|openai|none)", provider)
	}
}

// ValidateRealtimeKnobs checks voice/reasoning against a CONCRETE provider
// (gemini|openai). Empty voice/reasoning are allowed (means "keep current"). The
// per-provider knobs (model/voice/reasoning) only make sense for a concrete
// provider, so anything else is an error.
func ValidateRealtimeKnobs(provider, voice, reasoning string) error {
	switch strings.ToLower(strings.TrimSpace(provider)) {
	case "gemini":
		if voice != "" && !realtimeGeminiVoices[voice] {
			return fmt.Errorf("invalid gemini voice %q (Puck|Charon|Kore|Fenrir|Aoede)", voice)
		}
		if reasoning != "" && !realtimeGeminiThinking[reasoning] {
			return fmt.Errorf("invalid gemini thinking_level %q (MINIMAL|LOW|MEDIUM|HIGH)", reasoning)
		}
	case "openai":
		if voice != "" && !realtimeOpenAIVoices[voice] {
			return fmt.Errorf("invalid openai voice %q (alloy|ash|coral|echo|fable|onyx|nova|sage|shimmer)", voice)
		}
		if reasoning != "" && !realtimeOpenAIReasoning[reasoning] {
			return fmt.Errorf("invalid openai reasoning_effort %q (minimal|low|medium|high|xhigh)", reasoning)
		}
	default:
		return fmt.Errorf("realtime model/voice/reasoning require a concrete provider (gemini|openai), got %q", provider)
	}
	return nil
}
