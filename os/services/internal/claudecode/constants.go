package claudecode

// Wire constants for the Claude Code backend. Claude Code (the Anthropic CLI
// agent) has no server mode of its own, so the device runs a thin local bridge
// (materialized by presync.sh to /root/.claudecode/bridge.py, systemd unit
// claudecode.service). The bridge holds ONE persistent headless Claude Code
// process (`claude --print --input-format stream-json --output-format
// stream-json`) and exposes this WebSocket — os-server only acts as a client.
//
// Frame protocol over the socket:
//
//	os-server → bridge:  {"type":"message.send","id":..,"payload":{"content":..,
//	                      "attachments":[{"type":"image","url":"data:..;base64,.."}]}}
//	                     {"type":"session.new"}   → bridge restarts Claude fresh
//	                     {"type":"ping","id":..}  → {"type":"pong"}
//	bridge → os-server:  Claude Code stream-json events forwarded VERBATIM
//	                     (type: system / assistant / user / result), plus
//	                     {"type":"pong"} and {"type":"bridge.status",...} /
//	                     {"type":"bridge.error",...} bridge frames.
//
// The stream-json events are translated in translator.go into the same
// domain.WSEvent shape the OpenClaw handler consumes.
const (
	// WSURL is the local bridge WebSocket endpoint (see bridge.py in presync.sh).
	WSURL = "ws://127.0.0.1:18791/claude/ws/"

	// Token is the bearer token sent in the Authorization header on connect.
	// The bridge is seeded with the same value (presync.sh materializes it into
	// bridge.py) — a fixed device-local token, mirroring the picoclaw contract.
	Token = "autonomous_claudecode_token"

	// Conversation is a label only — Claude Code owns its session ids; the real
	// session UUID is captured from the stream-json `system:init` event.
	Conversation = "device-main"

	// claudecodeHome is the backend's device-local state dir: bridge.py, .env
	// (ANTHROPIC_* + channel launch flags, presync-owned), session.json, and the
	// workspace/ Claude Code runs in.
	claudecodeHome = "/root/.claudecode"
)
