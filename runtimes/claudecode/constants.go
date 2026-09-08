package claudecode

import "go.autonomous.ai/os/system/lib/syspath"

// Wire constants for the Claude Code backend. Claude Code (the Anthropic CLI
// agent) has no server mode of its own, so the device runs a thin local bridge:
// the Go gatewayd (runtimes/claudecode/gatewayd) compiled into the os-server
// binary and run as `os-server claudecode-gatewayd` (systemd unit
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
// Conversation is a label only — Claude Code owns its session ids; the real
// session UUID is captured from the stream-json `system:init` event.
const Conversation = "device-main"

// Resolved once at process start from the same env vars the gatewayd and
// presync.sh read (syspath). Unset env keeps the device defaults byte for byte.
var (
	// WSURL is the local bridge WebSocket endpoint (served by the gatewayd —
	// runtimes/claudecode/gatewayd, CLAUDECODE_PORT, default 18791).
	WSURL = "ws://127.0.0.1:" + syspath.ClaudeCodePort() + "/claude/ws/"

	// Token is the bearer token sent in the Authorization header on connect.
	// The gatewayd defaults to the same value — a fixed device-local token,
	// mirroring the picoclaw contract.
	Token = syspath.ClaudeCodeWSToken()

	// claudecodeHome is the backend's device-local state dir: .env
	// (ANTHROPIC_* + channel launch flags, presync-owned), session.json, and the
	// workspace/ Claude Code runs in. CLAUDECODE_HOME, default /root/.claudecode.
	claudecodeHome = syspath.ClaudeCodeHome()

	// EnvFile is the presync-owned launch env (ANTHROPIC_* creds + channel
	// flags). systemd injects it into the gatewayd only; the web CLI sources it
	// too so an interactive `claude` reuses the campaign key instead of
	// prompting login.
	EnvFile = claudecodeHome + "/.env"
)
