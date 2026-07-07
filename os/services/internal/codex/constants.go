package codex

// Wire constants for the Codex backend. The OpenAI Codex CLI has no server
// mode of its own, so the device runs a thin local bridge — compiled into the
// os-server binary (internal/codex/gatewayd, systemd unit codex.service runs
// `os-server codex-gatewayd`). The bridge spawns one `codex exec --json
// --dangerously-bypass-approvals-and-sandbox` subprocess per turn (resuming
// the persisted thread id) and exposes this WebSocket — the main os-server
// process only acts as a client.
//
// Frame protocol over the socket:
//
//	os-server → bridge:  {"type":"message.send","id":..,"payload":{"content":..,
//	                      "attachments":[{"type":"image","url":"data:..;base64,.."}]}}
//	                     {"type":"session.new"}   → bridge drops the thread id
//	                     {"type":"ping","id":..}  → {"type":"pong"}
//	bridge → os-server:  Codex exec JSONL events forwarded VERBATIM
//	                     (type: thread.started / turn.started / item.started /
//	                     item.completed / turn.completed / turn.failed), plus
//	                     {"type":"pong"} and {"type":"bridge.status",...} /
//	                     {"type":"bridge.error",...} bridge frames.
//
// The exec events are translated in translator.go into the same
// domain.WSEvent shape the OpenClaw handler consumes.
const (
	// WSURL is the local bridge WebSocket endpoint (see bridge.py, materialized
	// by presync.sh).
	WSURL = "ws://127.0.0.1:18792/codex/ws/"

	// Token is the bearer token sent in the Authorization header on connect.
	// The bridge reads the same value from /root/.codex/.env (CODEX_WS_TOKEN,
	// presync-owned) — a fixed device-local token, mirroring the picoclaw and
	// claudecode contracts.
	Token = "autonomous_codex_token"

	// Conversation is a label only — Codex owns its thread ids; the real
	// thread id is captured from the `thread.started` exec event.
	Conversation = "device-main"

	// codexHome is the backend's device-local state dir: CODEX_HOME for the
	// CLI (config.toml, auth, sessions/) plus the bridge.py, .env, session.json
	// and the workspace/ Codex runs in. Wiped whole by ResetAgent.
	codexHome = "/root/.codex"
)
