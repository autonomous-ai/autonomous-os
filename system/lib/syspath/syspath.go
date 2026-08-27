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

// AgentRuntimeHome is one runtime's state dir — where its workspace/ and
// media/hal-snapshots/ live. On a board every runtime sits under the agent
// user's home as /root/.<runtime>, and unset env keeps exactly that. Codex is
// resolved through CodexHome() instead of composed from AgentHome(): it owns a
// dedicated var that the gatewayd, presync.sh and HAL all read, and off-device
// that var points at the developer's real install while OS_AGENT_HOME points at
// throwaway state — composing the two would name a directory nobody writes to.
func AgentRuntimeHome(runtime string) string {
	if runtime == "codex" {
		return CodexHome()
	}
	return AgentHome() + "/." + runtime
}

// AgentStatePath records the agent-runtime switch history (persona migration).
func AgentStatePath() string {
	return envOr("OS_AGENT_STATE_PATH", "/root/config/agent_state.json")
}

// BackendUplink reports whether this process may talk to the Autonomous
// backend — the 15s status ping and the MQTT command channel.
//
// A board is the device it reports as, so this is on and stays on. An
// off-device run is NOT: the backend identifies a device by its llm_api_key,
// not by device_id, so a laptop holding a copy of a device's config.json is
// indistinguishable from that device. Measured 27/08/2026 with both running:
// the ping overwrote the real lamp's local_ip / mac / version / skills every
// 15s, and — because the client ID is derived from the device_id the backend
// hands back — the two MQTT clients kicked each other off the broker about
// once a second, indefinitely.
//
// Nothing a developer needs goes through here: the web UI, Flow Monitor, voice
// pipeline, agent and skills are all local. So `make os-dev` sets it off and a
// deliberate OS_BACKEND_UPLINK=on is the only way to aim a laptop at a real
// device's backend record.
func BackendUplink() bool {
	return envOr("OS_BACKEND_UPLINK", "on") != "off"
}

// LogFile is os-server's rotating log file.
func LogFile() string { return envOr("OS_LOG_FILE", "/var/log/os-server.log") }

// HALLogFile is the file HAL's rotating handler writes (hal/server_support/
// log_setup.py, $HAL_LOG_DIR/server.log). os-server only reads it, for the
// web UI's HAL log tab.
func HALLogFile() string { return envOr("OS_HAL_LOG_FILE", "/var/log/hal/server.log") }

// AgentBridgeLog is a file to read the agent bridge's output from instead of
// its systemd journal. Empty — the default — keeps the journal, which is what
// a board has and what every runtime's `journal:<unit>.service` mapping means.
// Off-device there is no systemd at all, so `make codex-dev` tees the bridge to
// a file and names it here; without this the web UI's Agent tabs are the only
// ones that stay blank on a laptop.
func AgentBridgeLog() string { return envOr("OS_AGENT_BRIDGE_LOG", "") }

// BootstrapConfig is the OTA worker's config file. os-server reads only
// metadata_url from it — the base for skill zips and the skill watcher.
func BootstrapConfig() string {
	return envOr("OS_BOOTSTRAP_CONFIG", "/root/config/bootstrap.json")
}
