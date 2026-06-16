package skills

import "go.autonomous.ai/os/internal/device"

// Hooks are runtime triggers (HOOK.md + handler.ts on the CDN) that fire
// automatically before a turn. They are a sibling mechanism to skills and gate
// the same way (see SupportedHooks) — also platform metadata, not runtime-coupled.

// Hooks is the full set of hooks the OS publishes.
var Hooks = []string{
	"emotion-acknowledge",
	"turn-gate",
}

// HookCapability maps a hook to the DEVICE.md capability it requires. A hook
// absent from this map has no hardware dependency and is always installed.
// emotion-acknowledge fires an expression every turn, so it needs `expression`
// (the body can show emotion): a device without it (e.g. a sensor-only box) must
// not register the hook, or it POSTs to a route the device never mounts.
var HookCapability = map[string]string{
	"emotion-acknowledge": device.CapExpression,
}

// SupportedHooks filters Hooks to those a device with deviceCaps can run, the
// same way Supported filters skills. Fail-open on empty deviceCaps.
func SupportedHooks(deviceCaps map[string]bool) []string {
	if len(deviceCaps) == 0 {
		return Hooks
	}
	out := make([]string, 0, len(Hooks))
	for _, name := range Hooks {
		if cap := HookCapability[name]; cap == "" || deviceCaps[cap] {
			out = append(out, name)
		}
	}
	return out
}
