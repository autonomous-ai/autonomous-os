package hermes

import (
	"log/slog"
	"strings"
)

// ApplyExternalEndpoint retargets Hermes at a server on another machine — the
// Intern is a voice/chat frontend for that server instead of the local install.
// Called by system/agent/factory.go on every gateway construction when
// config.agent_runtime == "remote", so a Mac IP change or an os-server restart
// picks up the newest config.json value.
//
// Empty inputs keep the current value: the token is optional (a Hermes bound
// only to LAN often needs none), and a call with an empty URL is a no-op so a
// user who switches to "remote" without filling the URL falls back to whatever
// was last saved rather than clobbering it with the default.
//
// Not concurrency-safe — the callers construct the gateway once at startup
// (single goroutine, factory.ProvideGateway) and the values are then read-only.
func ApplyExternalEndpoint(url, token string) {
	if u := strings.TrimSpace(url); u != "" {
		BaseURL = u
	}
	if t := strings.TrimSpace(token); t != "" {
		APIKey = t
	}
	slog.Info("hermes remote endpoint applied",
		"component", "hermes",
		"base_url", BaseURL,
		"api_key_set", APIKey != "",
	)
}
