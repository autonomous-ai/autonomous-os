package config

import (
	"strings"

	"go.autonomous.ai/os/system/lib/urlnorm"
)

// GELFRelayCredentials returns the cloud API base URL and device key that
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
// complete and points at our own gateway.
func autonomousRelayTarget(rawBase, rawKey string) (base, key string, ok bool) {
	base = withAPIVersion(strings.TrimRight(strings.TrimSpace(rawBase), "/"))
	key = strings.TrimSpace(rawKey)
	return base, key, key != "" && urlnorm.IsAutonomousHost(base)
}

// withAPIVersion adds the version segment an older config.json may lack:
// config.json is normalized only when os-server saves it, so a file that was
// never re-saved can still hold the unversioned base.
//
// Host-agnostic on purpose — the rule is "our own hosts end their AI base with
// the version", so it needs no hostname of its own, and HAL's copy
// (hal/drivers/gelf_handler.py) states it the same way.
func withAPIVersion(base string) string {
	if strings.HasSuffix(base, "/ai") && urlnorm.IsAutonomousHost(base) {
		return base + "/v1"
	}
	return base
}
