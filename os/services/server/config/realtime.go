package config

import "strings"

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
	defaultRealtimeGeminiModel     = "gemini-3.1-flash-live-preview"
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
func (c *Config) RealtimeBaseURL() string {
	if c.Realtime != nil && c.Realtime.BaseURL != "" {
		return c.Realtime.BaseURL
	}
	return c.LLMBaseURL
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
