#!/usr/bin/env bash
# os-dev-seed — prepare the off-device state dir for `make os-dev`.
#
# Creates <state dir>/config/config.json with the three keys a laptop run needs
# and cannot infer, then leaves the file alone on later runs (a dev's own edits
# survive). Nothing here installs a runtime: codex, its skills and AGENTS.md are
# expected to be in place already.
set -euo pipefail

STATE_DIR="${1:?usage: os-dev-seed.sh <state-dir> <device-type> <agent-runtime> <codex-home>}"
DEVICE_TYPE="${2:?}"
AGENT_RUNTIME="${3:?}"
CODEX_HOME="${4:?}"

log() { echo "[os-dev-seed] $*"; }

mkdir -p "$STATE_DIR/config"
CONFIG_JSON="$STATE_DIR/config/config.json"

# set_up_completed gates the whole startup sequence (server/config_watch.go):
# presync + EnsureOnboarding never run while it is false, so an off-device run
# would boot with an empty workspace and no explanation.
python3 - "$CONFIG_JSON" "$DEVICE_TYPE" "$AGENT_RUNTIME" <<'PY'
import json, os, sys
path, device_type, runtime = sys.argv[1:4]
cfg = {}
if os.path.exists(path):
    with open(path) as f:
        cfg = json.load(f)
cfg["device_type"] = device_type
cfg["agent_runtime"] = runtime
cfg["set_up_completed"] = True
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print(f"[os-dev-seed] {path}: device_type={device_type} agent_runtime={runtime} set_up_completed=true")
PY

# presync.sh regenerates config.toml from config.json on every boot and keeps
# only [mcp_servers.*]. Back up a hand-written one once so pointing CODEX_HOME
# at a real install is not a one-way door.
if [ "$AGENT_RUNTIME" = "codex" ] && [ -f "$CODEX_HOME/config.toml" ] && [ ! -f "$CODEX_HOME/config.toml.pre-os-dev" ]; then
  cp "$CODEX_HOME/config.toml" "$CODEX_HOME/config.toml.pre-os-dev"
  log "backed up $CODEX_HOME/config.toml → config.toml.pre-os-dev (presync rewrites it)"
fi

log "state dir ready: $STATE_DIR"
