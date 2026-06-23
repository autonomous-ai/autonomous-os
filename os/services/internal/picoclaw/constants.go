package picoclaw

// Wire constants for the PicoClaw backend. PicoClaw is assumed already running
// on the Pi as a systemd service exposing a WebSocket endpoint — os-server only
// acts as a client (no onboarding / install path; see docs/picoclaw.md).
const (
	// WSURL is the PicoClaw WebSocket endpoint. PicoClaw speaks a single
	// message-oriented protocol over this socket (message.send out;
	// typing.* / message.* / error / pong in).
	WSURL = "ws://127.0.0.1:18790/pico/ws/"

	// Token is the bearer token sent in the Authorization header on connect.
	// PicoClaw owns its own auth; this is a fixed device-local token (the
	// PicoClaw systemd unit is seeded with the same value out of band).
	Token = "darren_pico_token"

	// Conversation is the default session name everything flows into until the
	// server assigns a session_id on its first frame.
	Conversation = "device-main"
)
