package device

import (
	"fmt"
	"log/slog"
	"strings"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

// validateRealtimeSet checks a realtime payload before any write: the provider
// selector, and (when per-provider knobs are present) the target provider's
// voice/reasoning. The target is the provider being set, or the current one when
// `provider` is omitted. Returns a descriptive error; nothing is written.
func (s *Service) validateRealtimeSet(d domain.RealtimeSetData) error {
	if err := config.ValidateRealtimeProvider(d.Provider); err != nil {
		return err
	}
	if d.Model != "" || d.Voice != "" || d.Reasoning != "" {
		target := strings.TrimSpace(d.Provider)
		if target == "" {
			target = s.config.RealtimeProvider() // current resolved provider
		}
		if err := config.ValidateRealtimeKnobs(target, d.Voice, d.Reasoning); err != nil {
			return err
		}
	}
	return nil
}

// applyRealtimeSet mutates the `realtime` block in c per the payload. Caller must
// have run validateRealtimeSet first; this only writes. Empty/omitted fields leave
// the current value unchanged; per-provider knobs land in the active provider's
// sub-object. Must run inside WithLockSave.
func applyRealtimeSet(c *config.Config, d domain.RealtimeSetData) {
	if c.Realtime == nil {
		c.Realtime = config.DefaultRealtimeConfig()
	}
	rt := c.Realtime
	if d.Enabled != nil {
		rt.Enabled = d.Enabled
	}
	if d.Provider != "" {
		rt.Provider = strings.ToLower(strings.TrimSpace(d.Provider))
	}
	// Credentials are provider-routed: qwen and the cascaded brains keep their own
	// api_key/base_url in their sub-object (HAL deliberately ignores the shared
	// fields for both — those hold the campaign-api credentials gemini/openai use).
	switch strings.ToLower(strings.TrimSpace(rt.Provider)) {
	case "qwen":
		if d.APIKey != "" || d.BaseURL != "" {
			if rt.Qwen == nil {
				rt.Qwen = &config.QwenRealtime{}
			}
			if d.APIKey != "" {
				rt.Qwen.APIKey = d.APIKey
			}
			if d.BaseURL != "" {
				rt.Qwen.BaseURL = d.BaseURL
			}
		}
	case "pipecat", "cascaded":
		if d.APIKey != "" || d.BaseURL != "" {
			if rt.Pipecat == nil {
				rt.Pipecat = &config.PipecatRealtime{}
			}
			if d.APIKey != "" {
				rt.Pipecat.APIKey = d.APIKey
			}
			if d.BaseURL != "" {
				rt.Pipecat.BaseURL = d.BaseURL
			}
		}
	default:
		if d.APIKey != "" {
			rt.APIKey = d.APIKey
		}
		if d.BaseURL != "" {
			rt.BaseURL = d.BaseURL
		}
	}
	if d.Model == "" && d.Voice == "" && d.Reasoning == "" {
		return
	}
	switch strings.ToLower(strings.TrimSpace(rt.Provider)) {
	case "gemini":
		if rt.Gemini == nil {
			rt.Gemini = &config.GeminiRealtime{}
		}
		if d.Model != "" {
			rt.Gemini.Model = d.Model
		}
		if d.Voice != "" {
			rt.Gemini.Voice = d.Voice
		}
		if d.Reasoning != "" {
			rt.Gemini.ThinkingLevel = d.Reasoning
		}
	case "openai":
		if rt.OpenAI == nil {
			rt.OpenAI = &config.OpenAIRealtime{}
		}
		if d.Model != "" {
			rt.OpenAI.Model = d.Model
		}
		if d.Voice != "" {
			rt.OpenAI.Voice = d.Voice
		}
		if d.Reasoning != "" {
			rt.OpenAI.ReasoningEffort = d.Reasoning
		}
	case "qwen":
		if rt.Qwen == nil {
			rt.Qwen = &config.QwenRealtime{}
		}
		if d.Model != "" {
			rt.Qwen.Model = d.Model
		}
		if d.Voice != "" {
			rt.Qwen.Voice = d.Voice
		}
	// no reasoning knob — validateRealtimeSet already rejected it
	case "pipecat", "cascaded":
		if rt.Pipecat == nil {
			rt.Pipecat = &config.PipecatRealtime{}
		}
		if d.Model != "" {
			rt.Pipecat.Model = d.Model
		}
		// no voice/reasoning knobs — validateRealtimeSet already rejected them
	}
}

// UpdateRealtimeConfig applies a realtime payload (MQTT realtime.set or the HTTP
// `realtime` field) to config.json under the config lock, then restarts hal so it
// reads the new block (HAL reads config.json at import).
func (s *Service) UpdateRealtimeConfig(d domain.RealtimeSetData) error {
	if err := s.validateRealtimeSet(d); err != nil {
		return err
	}
	if err := s.config.WithLockSave(func(c *config.Config) {
		applyRealtimeSet(c, d)
	}); err != nil {
		return fmt.Errorf("save config: %w", err)
	}
	slog.Info("realtime config updated", "component", "device",
		"provider", s.config.RealtimeProvider(), "enabled", s.config.RealtimeEnabled())
	s.restartHAL("realtime config change")
	return nil
}
