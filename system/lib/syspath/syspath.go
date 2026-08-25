// Package syspath resolves the device-absolute paths os-server owns.
//
// Every accessor keeps its production default and is overridden by a single
// env var, so the SAME binary that ships to the board also runs off-device
// (`make os-dev`) — no build tag, no second code path. HAL already reads
// OS_CONFIG_PATH / HAL_USERS_DIR this way; runtimes/codex/gatewayd and
// presync.sh already read CODEX_HOME / CODEX_PORT. This package closes the
// gap on the os-server side, which had them as Go consts.
//
// Unset env = today's device behaviour, byte for byte.
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
// sessions/ and the workspace/ codex runs in. Same var the gatewayd reads.
func CodexHome() string { return envOr("CODEX_HOME", "/root/.codex") }

// CodexPort is the loopback port codex-gatewayd listens on.
func CodexPort() string { return envOr("CODEX_PORT", "18792") }

// CodexWSToken is the bearer token os-server sends to the bridge. The bridge
// reads the same value from $CODEX_HOME/.env (presync-owned).
func CodexWSToken() string { return envOr("CODEX_WS_TOKEN", "autonomous_codex_token") }

// AgentHome is the agent user's home dir — the root a Telegram coding session
// resolves "~" and relative folders against.
func AgentHome() string { return envOr("OS_AGENT_HOME", "/root") }

// AgentStatePath records the agent-runtime switch history (persona migration).
func AgentStatePath() string {
	return envOr("OS_AGENT_STATE_PATH", "/root/config/agent_state.json")
}

// LogFile is os-server's rotating log file.
func LogFile() string { return envOr("OS_LOG_FILE", "/var/log/os-server.log") }

// BootstrapConfig is the OTA worker's config file. os-server reads only
// metadata_url from it — the base for skill zips and the skill watcher.
func BootstrapConfig() string {
	return envOr("OS_BOOTSTRAP_CONFIG", "/root/config/bootstrap.json")
}
