// Package syspath resolves the absolute paths os-server owns.
//
// Each accessor has the board's path as its default and one env var to
// override it, so the same binary runs on a device and on a laptop
// (`make os-dev`) — no build tag, no second code path.
//
// Unset env = device behaviour, byte for byte.
package syspath

import "os"

// envOr returns the env value for key, or def when unset/empty.
func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// CodexHome is Codex's state dir: config.toml, auth.json, .env, skills/,
// sessions/, workspace/. Same var the gatewayd and presync.sh read.
func CodexHome() string { return envOr("CODEX_HOME", "/root/.codex") }

// CodexPort is the loopback port codex-gatewayd listens on.
func CodexPort() string { return envOr("CODEX_PORT", "18792") }

// CodexWSToken is the bearer token os-server sends to the bridge. The bridge
// reads the same value from $CODEX_HOME/.env.
func CodexWSToken() string { return envOr("CODEX_WS_TOKEN", "autonomous_codex_token") }

// ClaudeCodeHome is Claude Code's state dir: .env, session.json, workspace/.
// Unlike CodexHome it defaults under AgentHome() — it is device state, not a
// developer install, so off-device it follows OS_AGENT_HOME.
func ClaudeCodeHome() string { return envOr("CLAUDECODE_HOME", AgentHome()+"/.claudecode") }

// ClaudeCodeUserDir is the claude CLI's user dir: skills, credentials,
// projects. It is $HOME/.claude of the child process, so it must track the HOME
// the gatewayd asserts (gatewayd.Config.Home = OS_AGENT_HOME).
func ClaudeCodeUserDir() string { return AgentHome() + "/.claude" }

// ClaudeCodePort is the loopback port claudecode-gatewayd listens on.
func ClaudeCodePort() string { return envOr("CLAUDECODE_PORT", "18791") }

// ClaudeCodeWSToken is the bearer token os-server sends to the bridge. The
// bridge defaults to the same value (runtimes/claudecode.Token).
func ClaudeCodeWSToken() string {
	return envOr("CLAUDECODE_WS_TOKEN", "autonomous_claudecode_token")
}

// AgentHome is the agent user's home — what a Telegram coding session resolves
// "~" and relative folders against.
func AgentHome() string { return envOr("OS_AGENT_HOME", "/root") }

// AgentRuntimeHome is one runtime's state dir, holding its workspace/ and
// media/hal-snapshots/. On a board every runtime sits at /root/.<runtime> —
// what the last line returns. Codex and claudecode go through their own
// accessors because each owns an env var the gatewayd, presync.sh and HAL read
// too, and off-device CODEX_HOME points at the developer's real install rather
// than under OS_AGENT_HOME.
func AgentRuntimeHome(runtime string) string {
	switch runtime {
	case "codex":
		return CodexHome()
	case "claudecode":
		return ClaudeCodeHome()
	}
	return AgentHome() + "/." + runtime
}

// AgentStatePath records the agent-runtime switch history (persona migration).
func AgentStatePath() string {
	return envOr("OS_AGENT_STATE_PATH", "/root/config/agent_state.json")
}

// BackendUplink reports whether this process may talk to the Autonomous backend
// — the 15s status ping and the MQTT command channel.
//
// The backend identifies a device by its llm_api_key, not its device_id, so a
// laptop holding a copy of a device's config.json is indistinguishable from
// that device. Measured 27/08/2026 with both running: the ping overwrote the
// real lamp's local_ip / mac / version / skills every 15s, and the two MQTT
// clients kicked each other off the broker about once a second.
//
// Nothing a developer needs goes through here, so `make os-dev` turns it off.
// OS_BACKEND_UPLINK=on is the only way back.
func BackendUplink() bool {
	return envOr("OS_BACKEND_UPLINK", "on") != "off"
}

// LogFile is os-server's rotating log file.
func LogFile() string { return envOr("OS_LOG_FILE", "/var/log/os-server.log") }

// HALLogFile is the file HAL's rotating handler writes. os-server only reads
// it, for the web UI's HAL log tab.
func HALLogFile() string { return envOr("OS_HAL_LOG_FILE", "/var/log/hal/server.log") }

// AgentBridgeLog is a file to read the agent bridge's output from instead of
// its systemd journal. Empty — the default — keeps the journal, which is what a
// board has. Off-device there is no systemd, so `make codex-dev` tees the
// bridge to a file and names it here.
func AgentBridgeLog() string { return envOr("OS_AGENT_BRIDGE_LOG", "") }

// BootstrapConfig is the OTA worker's config file. os-server reads only
// metadata_url from it — the base for skill zips and the skill watcher.
func BootstrapConfig() string {
	return envOr("OS_BOOTSTRAP_CONFIG", "/root/config/bootstrap.json")
}
