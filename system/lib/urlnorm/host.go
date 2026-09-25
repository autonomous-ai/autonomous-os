package urlnorm

import (
	"net/url"
	"strings"
)

// autonomousHosts are the registrable domains that serve our own gateway
// (*.autonomous.ai in production, *.autonomousdev.xyz in staging).
var autonomousHosts = []string{"autonomous.ai", "autonomousdev.xyz"}

// IsAutonomousHost reports whether baseURL points at an Autonomous-operated
// host. Strict by design: empty, unparseable or host-less input is false, so a
// caller about to send device data there never does it on a guess. (openclaw's
// BYO check, isAutonomousEndpoint, answers true for those instead — its safe
// default is "keep the hosted path", the opposite question.)
func IsAutonomousHost(baseURL string) bool {
	u, err := url.Parse(strings.TrimSpace(baseURL))
	if err != nil || u.Host == "" {
		return false
	}
	host := strings.ToLower(u.Hostname())
	for _, domain := range autonomousHosts {
		if host == domain || strings.HasSuffix(host, "."+domain) {
			return true
		}
	}
	return false
}
