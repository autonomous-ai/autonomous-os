package config

import "strings"

// JevIntentConfig exposes a kill switch and a bounded classification budget.
// URL/authentication reuse config.json's llm_base_url and llm_api_key.
type JevIntentConfig struct {
	Enabled   *bool `json:"enabled,omitempty" yaml:"enabled"`
	TimeoutMS int   `json:"timeout_ms,omitempty" yaml:"timeoutMs"`
}

// JevIntentSettings is a resolved snapshot of proxy settings and defaults.
type JevIntentSettings struct {
	Enabled   bool
	TimeoutMS int
	Endpoint  string
	APIKey    string
}

// JevIntentSettings resolves config.json without any environment credentials.
// local_intent=false remains the master switch; Jev never replaces local rules
// or runs on its own.
func (c *Config) JevIntentSettings() JevIntentSettings {
	settings := JevIntentSettings{
		TimeoutMS: 350,
		APIKey:    strings.TrimSpace(c.LLMAPIKey),
	}
	// Proposed BFF contract, mirroring /chat/completions on the same base.
	// Until BFF deploys this route, leave enabled=false; a 404 falls through.
	if base := strings.TrimRight(strings.TrimSpace(c.LLMBaseURL), "/"); base != "" {
		settings.Endpoint = base + "/jev/decisions"
	}
	if c.JevIntent != nil {
		settings.TimeoutMS = c.JevIntent.TimeoutMS
		if c.JevIntent.Enabled != nil {
			settings.Enabled = *c.JevIntent.Enabled
		}
	}
	settings.Enabled = settings.Enabled && c.LocalIntentEnabled()
	if settings.TimeoutMS <= 0 {
		settings.TimeoutMS = 350
	} else if settings.TimeoutMS > 1000 {
		settings.TimeoutMS = 1000
	}
	return settings
}
