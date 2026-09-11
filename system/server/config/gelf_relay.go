package config

import (
	"strings"

	"go.autonomous.ai/os/system/lib/urlnorm"
)

// GELFRelayCredentials returns the campaign-api base URL and device key that
// os-server's log relay (logger.EnableGELFRelay) ships through, or two empty
// strings when the device has no Autonomous credential to use.
//
// Logs must only ever reach our own gateway. Once an owner points the AI Brain
// at their own provider, LLMBaseURL is THEIR endpoint, so the shipped
// AutonomousDefaults win; they are captured only on the first credential
// change, so an untouched device has none and its live fields are still the
// shipped set. A candidate is used only when it is complete and on an
// Autonomous host, which also covers a device that was BYO from first boot and
// therefore has no shipped set at all.
func (c *Config) GELFRelayCredentials() (baseURL, apiKey string) {
	if d := c.AutonomousDefaults; d != nil {
		if base, key, ok := autonomousRelayTarget(d.BaseURL, d.APIKey); ok {
			return base, key
		}
	}
	if base, key, ok := autonomousRelayTarget(c.LLMBaseURL, c.LLMAPIKey); ok {
		return base, key
	}
	return "", ""
}

// autonomousRelayTarget normalizes one base/key pair and reports whether it is
// complete and points at our own gateway. Normalizing here matters because
// config.json is normalized only when saved, so a file that was never re-saved
// can still hold the unversioned campaign-api base.
func autonomousRelayTarget(rawBase, rawKey string) (base, key string, ok bool) {
	base = urlnorm.NormalizeBaseURL(rawBase)
	key = strings.TrimSpace(rawKey)
	return base, key, key != "" && urlnorm.IsAutonomousHost(base)
}
