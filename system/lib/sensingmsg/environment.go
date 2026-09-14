package sensingmsg

import "sync"

var environmentReplay struct {
	sync.RWMutex
	allowed func() bool
}

// SetEnvironmentReplayAllowed supplies the current device capability and sleep
// policy to runtime queues without coupling a runtime to the HTTP handler.
func SetEnvironmentReplayAllowed(allowed func() bool) {
	environmentReplay.Lock()
	environmentReplay.allowed = allowed
	environmentReplay.Unlock()
}

// ReplayAllowed rechecks environment policy after a queued event has waited.
// An unconfigured process must not emit optional environmental notifications.
func ReplayAllowed(eventType string) bool {
	if eventType != "environment.update" {
		return true
	}
	environmentReplay.RLock()
	allowed := environmentReplay.allowed
	environmentReplay.RUnlock()
	return allowed != nil && allowed()
}
