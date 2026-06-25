package httpapi

import "errors"

// Ports — the interfaces the delivery layer depends on. Concrete
// implementations are provided by package main, so the HTTP layer never imports
// the BLE / state-machine internals (dependency inversion).

// StatusProvider exposes a read-only snapshot of buddy state.
type StatusProvider interface {
	Status() Status
}

// Status is a transport-agnostic snapshot of the device's buddy state.
type Status struct {
	State           string
	Connected       bool
	SessionsRunning int
	TokensToday     int
	HasPending      bool
	PendingID       string
	PendingTool     string
	PendingHint     string
}

// ApprovalService runs the voice-approval use case: validate the pending
// prompt, relay the decision over BLE, and update the counters.
type ApprovalService interface {
	Approve(id string) error
	Deny(id string) error
}

// Sentinel errors the approval handler maps to HTTP 409 Conflict.
var (
	ErrNoPending      = errors.New("no pending prompt")
	ErrPromptMismatch = errors.New("prompt id mismatch")
)

// ActivitySink receives Claude Code activity pushed by the plugin. The current
// implementation just logs; the device bridge (HAL: LED / display / voice) will
// implement this same interface later without touching the delivery layer.
type ActivitySink interface {
	Notify(level, title, subtitle string, sound bool)
	Usage(fiveHour, sevenDay int, reset5h, reset7d string, sound bool)
}
