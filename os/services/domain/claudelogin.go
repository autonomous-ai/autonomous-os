package domain

import "context"

// ClaudeLoginPairer is the OPTIONAL gateway surface for the claude.ai OAuth
// login flow. Only the claudecode runtime implements it (the device runs
// `claude setup-token` and adopts the resulting subscription credentials
// instead of the config.json API key); callers type-assert the active
// AgentGateway and treat a failed assertion as "not supported on this runtime"
// — the same optional-interface pattern as SlackBridge (slack_bridge.go), so
// the other backends stay untouched.
//
// Flow shape mirrors PairWhatsapp (streaming PairingEvents, caller MUST drain)
// with one extra leg: the OAuth code travels BACK into the flow via
// SubmitClaudeLoginCode.
//
//	pairing_starting → pairing_url (URL field) → success | timeout | failure
type ClaudeLoginPairer interface {
	// StartClaudeLogin launches the OAuth login flow and emits PairingEvents on
	// the returned channel. At most one flow may be active; concurrent calls
	// return a one-event channel containing PairingStatusFailure with error
	// "login_already_in_progress".
	StartClaudeLogin(ctx context.Context) <-chan PairingEvent

	// SubmitClaudeLoginCode feeds the authorization code the user copied from
	// the browser back into the waiting login flow. Errors when no flow is
	// awaiting a code.
	SubmitClaudeLoginCode(code string) error
}
