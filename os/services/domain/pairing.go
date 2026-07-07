package domain

import "time"

// PairingEventStatus enumerates the lifecycle states a streaming pairing/login
// flow can publish (WhatsApp QR pairing, claude.ai OAuth login). Wire-format
// identifiers are intentionally lowercase + snake so they ride directly in the
// MQTT response Status field.
type PairingEventStatus string

const (
	PairingStatusStarting PairingEventStatus = "pairing_starting"
	PairingStatusQR       PairingEventStatus = "pairing_qr"
	// PairingStatusURL is the claude.ai OAuth login analog of pairing_qr: the
	// flow produced an authorization URL the user must open in a browser (the
	// URL rides in PairingEvent.URL). The flow then waits for the code to come
	// back via ClaudeLoginPairer.SubmitClaudeLoginCode.
	PairingStatusURL PairingEventStatus = "pairing_url"
	// PairingStatusSuccess is the single "ready" terminal status. For WhatsApp
	// it is emitted both for first-time pairing (after the post-pair Baileys
	// sync) and for resumed sessions (creds already on disk; no QR was shown).
	PairingStatusSuccess PairingEventStatus = "success"
	PairingStatusTimeout PairingEventStatus = "timeout"
	PairingStatusFailure PairingEventStatus = "failure"
)

// PairingEvent is one update from a streaming pairing/login flow.
// QRText / QRSeq / ExpiresAt are populated only when Status == PairingStatusQR;
// URL only when Status == PairingStatusURL.
type PairingEvent struct {
	Status    PairingEventStatus `json:"status"`
	QRText    string             `json:"qr_text,omitempty"`
	QRSeq     int                `json:"qr_seq,omitempty"`
	URL       string             `json:"url,omitempty"`
	ExpiresAt time.Time          `json:"expires_at,omitempty"`
	Error     string             `json:"error,omitempty"`
}
