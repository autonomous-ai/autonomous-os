package domain

import "time"

// PairingEventStatus enumerates the lifecycle states the WhatsApp pairing
// flow can publish. Wire-format identifiers are intentionally lowercase + snake
// so they ride directly in the MQTT response Status field.
type PairingEventStatus string

const (
	PairingStatusStarting PairingEventStatus = "pairing_starting"
	PairingStatusQR       PairingEventStatus = "pairing_qr"
	// PairingStatusSuccess is the single "channel ready to send" terminal status.
	// Emitted both for first-time pairing (after the post-pair Baileys sync) and
	// for resumed sessions (creds already on disk; no QR was shown).
	PairingStatusSuccess PairingEventStatus = "success"
	PairingStatusTimeout PairingEventStatus = "timeout"
	PairingStatusFailure PairingEventStatus = "failure"
)

// PairingEvent is one update from a streaming WhatsApp pairing flow.
// QRText / QRSeq / ExpiresAt are populated only when Status == PairingStatusQR.
type PairingEvent struct {
	Status    PairingEventStatus `json:"status"`
	QRText    string             `json:"qr_text,omitempty"`
	QRSeq     int                `json:"qr_seq,omitempty"`
	ExpiresAt time.Time          `json:"expires_at,omitempty"`
	Error     string             `json:"error,omitempty"`
}
