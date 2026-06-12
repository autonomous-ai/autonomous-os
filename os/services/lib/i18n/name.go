package i18n

import (
	"strings"
	"sync"
)

// Device/agent name injected into i18n strings so nothing hardcodes a device name.
// Two placeholders:
//
//	{name} — lowercase, for input matchers   (e.g. "hi {name}"   -> "hi <name>")
//	{Name} — display/title, for spoken text  (e.g. "{Name} đây"  -> "<Name> đây")
//
// Device-agnostic: each device supplies its own name — the agent name from
// IDENTITY.md when known, its device_type at startup as the fallback — never a
// compiled-in device name. Both placeholders resolve to the same name (lower /
// title cased), so a device's strings render with its own identity.
var (
	deviceNameMu      sync.RWMutex
	deviceNameLower   string
	deviceNameDisplay string
)

// SetDeviceName sets the name used to fill {name}/{Name} across i18n strings and
// rebuilds the chitchat wake-word strip list. Call at startup (device_type) and
// on agent rename (IDENTITY.md name). "" is ignored.
func SetDeviceName(name string) {
	n := strings.ToLower(strings.TrimSpace(name))
	if n == "" {
		return
	}
	disp := strings.ToUpper(n[:1]) + n[1:]
	deviceNameMu.Lock()
	deviceNameLower = n
	deviceNameDisplay = disp
	deviceNameMu.Unlock()
	SetChitchatWakeWords(BuildChitchatWakeWords(n))
}

// applyName fills {Name}/{name} placeholders with the current device name. Safe
// (no-op) on strings without placeholders and before any name is set.
func applyName(s string) string {
	deviceNameMu.RLock()
	lower, disp := deviceNameLower, deviceNameDisplay
	deviceNameMu.RUnlock()
	if disp != "" {
		s = strings.ReplaceAll(s, "{Name}", disp)
	}
	if lower != "" {
		s = strings.ReplaceAll(s, "{name}", lower)
	}
	return s
}

// applyNameAll returns a new slice with applyName applied to each element. nil in
// -> nil out (preserves "no pool" semantics for callers that test for nil).
func applyNameAll(in []string) []string {
	if in == nil {
		return nil
	}
	out := make([]string, len(in))
	for i, s := range in {
		out[i] = applyName(s)
	}
	return out
}
