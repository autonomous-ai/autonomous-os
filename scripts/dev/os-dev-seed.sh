#!/usr/bin/env bash
# os-dev-seed — prepare the off-device state dir for `make os-dev`.
#
# Creates <state dir>/config/config.json with the three keys a laptop run needs
# and cannot infer, then leaves the file alone on later runs (a dev's own edits
# survive). Nothing here installs a runtime: the CLI (codex / claude), its skills
# and its memory files are expected to be in place already.
set -euo pipefail

STATE_DIR="${1:?usage: os-dev-seed.sh <state-dir> <device-type> <agent-runtime> <codex-home> <claudecode-home>}"
DEVICE_TYPE="${2:?}"
# Empty = "no explicit choice": keep whatever config.json already says. The
# Makefile normally resolves this first (env > config > codex), so this is the
# belt-and-braces half of the same cascade for a direct script call.
AGENT_RUNTIME="${3:-}"
CODEX_HOME="${4:?}"
CLAUDECODE_HOME="${5:-$STATE_DIR/.claudecode}"

log() { echo "[os-dev-seed] $*"; }

mkdir -p "$STATE_DIR/config"
CONFIG_JSON="$STATE_DIR/config/config.json"
CONFIG_EXAMPLE="$(dirname "$0")/config.example.json"

# The config is the developer's to write — this script never invents one. Silently
# creating a near-empty file was worse than stopping: os-server booted, answered
# nothing useful, and nothing pointed back at the missing credentials.
if [ ! -f "$CONFIG_JSON" ]; then
  log "ERROR: $CONFIG_JSON not found."
  log "Copy the template and fill it in, then run again:"
  log "    cp $CONFIG_EXAMPLE $CONFIG_JSON"
  log "    \$EDITOR $CONFIG_JSON        # set llm_api_key + llm_base_url"
  exit 1
fi

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
# Explicit choice wins; otherwise keep the config's own value; codex only when
# neither exists. Never reset a deliberate switch just because the flag was
# omitted on a later run.
if runtime:
    cfg["agent_runtime"] = runtime
elif not str(cfg.get("agent_runtime", "")).strip():
    cfg["agent_runtime"] = "codex"
runtime = cfg["agent_runtime"]
cfg["set_up_completed"] = True
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print(f"[os-dev-seed] {path}: device_type={device_type} agent_runtime={runtime} set_up_completed=true")

# Name what each missing credential costs, at the moment the dev can act on it.
# A silent empty key surfaces much later as "the device never speaks" or a turn
# that answers blind, and neither points back here.
missing = []
if not str(cfg.get("llm_api_key", "")).strip() or not str(cfg.get("llm_base_url", "")).strip():
    missing.append("llm_api_key + llm_base_url — no TTS (silent replies), no STT, "
                   "no Gemini Live, no image description. The agent still answers text.")
if not str(cfg.get("admin_password_hash", "")).strip():
    missing.append("admin_password_hash — cannot log into the web UI (make web-dev). "
                   "Store a bcrypt hash, not the password.")
for m in missing:
    print(f"[os-dev-seed] NOTE: {m}")
PY

# Re-read what the cascade actually settled on — the branches below key off it,
# and it differs from $3 whenever the runtime came from config.json.
AGENT_RUNTIME="$(sed -n 's/.*"agent_runtime"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG_JSON" | head -1)"

# presync.sh regenerates config.toml from config.json on every boot and keeps
# only [mcp_servers.*]. Back up a hand-written one once so pointing CODEX_HOME
# at a real install is not a one-way door.
if [ "$AGENT_RUNTIME" = "codex" ] && [ -f "$CODEX_HOME/config.toml" ] && [ ! -f "$CODEX_HOME/config.toml.pre-os-dev" ]; then
  cp "$CODEX_HOME/config.toml" "$CODEX_HOME/config.toml.pre-os-dev"
  log "backed up $CODEX_HOME/config.toml → config.toml.pre-os-dev (presync rewrites it)"
fi

# claudecode keeps ALL its state inside the off-device dirs — never the
# developer's own ~/.claude. presync creates them too, but doing it here means
# the paths are printed before anything runs, so an accidental
# CLAUDECODE_HOME=$HOME/.claudecode is visible rather than discovered later.
if [ "$AGENT_RUNTIME" = "claudecode" ]; then
  CLAUDE_USER_DIR="$STATE_DIR/.claude"
  case "$CLAUDECODE_HOME" in
    "$HOME"/.claudecode|"$HOME"/.claude)
      log "ERROR: CLAUDECODE_HOME=$CLAUDECODE_HOME is inside your real \$HOME."
      log "That would mix the device's Claude Code state with your own. Aborting."
      exit 1
      ;;
  esac
  mkdir -p "$CLAUDECODE_HOME/workspace" "$CLAUDE_USER_DIR/skills"
  log "claudecode state: $CLAUDECODE_HOME"
  log "claude user dir:  $CLAUDE_USER_DIR (separate from $HOME/.claude)"
  if ! python3 -c "import json,sys;c=json.load(open(sys.argv[1]));sys.exit(0 if (str(c.get('claude_code_oauth_token','')).strip() or (str(c.get('llm_api_key','')).strip() and str(c.get('llm_base_url','')).strip())) else 1)" "$CONFIG_JSON"; then
    log "NOTE: no claude auth in $CONFIG_JSON — set llm_api_key + llm_base_url"
    log "      (api-key mode, campaign-api) or claude_code_oauth_token (subscription)."
  fi
fi

# bootstrap.json carries metadata_url, the ONLY thing os-server reads from that
# file (skill zip base + skill watcher). Without it downloadSkills logs
# "no ota_metadata_url configured" and the workspace stays skill-less. The bucket
# values come from the release scripts' single edit point, so the dev URL cannot
# drift from what upload-skills.sh publishes. Seeded once — a dev's edit survives.
BOOTSTRAP_JSON="$STATE_DIR/config/bootstrap.json"
if [ ! -f "$BOOTSTRAP_JSON" ]; then
  # shellcheck source=/dev/null
  . "$(dirname "$0")/../release/ota-config.sh"
  URL="${OTA_METADATA_URL:-https://storage.googleapis.com/${GCS_BUCKET}/${BUCKET_PREFIX}/ota/metadata.json}"
  printf '{\n  "metadata_url": "%s"\n}\n' "$URL" > "$BOOTSTRAP_JSON"
  log "seeded $BOOTSTRAP_JSON (metadata_url=$URL)"
fi

log "state dir ready: $STATE_DIR"
