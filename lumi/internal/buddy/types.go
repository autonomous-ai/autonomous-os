// Package buddy implements the lamp-side coordinator for the Lamp Buddy macOS
// companion app. It owns:
//   - pairing flow (6-digit code → long-lived token)
//   - persistent pairing record (config/buddies.json)
//   - WebSocket gateway to the connected buddy
//   - command dispatch with request/response matching by ID
//
// The HTTP delivery layer lives in server/buddy/delivery/http and is the only
// caller of this package.
package buddy

import (
	"crypto/rand"
	"encoding/hex"
)

// Command matches the JSON shape the buddy expects on its WebSocket.
// Mirrors lamp-buddy/mock-lamp/command.go.
type Command struct {
	ID        string         `json:"id"`
	Action    string         `json:"action"`
	Params    map[string]any `json:"params"`
	TimeoutMs int            `json:"timeout_ms,omitempty"`
	IssuedAt  string         `json:"issued_at,omitempty"`
	IssuedBy  string         `json:"issued_by,omitempty"`
}

// CommandResponse is the JSON shape the buddy returns over WebSocket.
type CommandResponse struct {
	ID         string         `json:"id"`
	OK         bool           `json:"ok"`
	Result     map[string]any `json:"result,omitempty"`
	Error      string         `json:"error,omitempty"`
	DurationMs int            `json:"duration_ms"`
}

// NewCommandID returns a fresh 16-hex-char ID for a command.
func NewCommandID() string {
	b := make([]byte, 8)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}
