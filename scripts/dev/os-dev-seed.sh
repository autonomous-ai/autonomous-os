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
CONFIG_EXAMPLE="$(dirname "$0")/config.example.json"

# No config yet: copy the template and carry on. The defaults below fill in
# everything a laptop run can infer (base URL, model, admin password hash), so
# the only key left for the developer is llm_api_key — named in the NOTE at the
# end of this run. Stopping here instead used to cost a round trip for a file
# whose content this script already knows.
if [ ! -f "$CONFIG_JSON" ]; then
  cp "$CONFIG_EXAMPLE" "$CONFIG_JSON"
  chmod 600 "$CONFIG_JSON"
  log "created $CONFIG_JSON from $(basename "$CONFIG_EXAMPLE")"
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
cfg["agent_runtime"] = runtime
cfg["set_up_completed"] = True
# Off-device only: log into the web UI without hand-generating a bcrypt hash.
# The hash below is bcrypt(cost 10) of "autonomous" and is public by definition
# — a real device gets its hash from setup, and this script never runs there.
# An existing hash (a config copied from a provisioned device) is left alone.
if not str(cfg.get("admin_password_hash", "")).strip():
    cfg["admin_password_hash"] = "$2a$10$nfmV4leY9FjNIS44X8/97OobRW6VWOvyhKYxAvLPAWTVKmCMWeOH6"
    print("[os-dev-seed] admin_password_hash was empty — defaulted to password 'autonomous'")
# The shared backend every dev key is issued against. Only filled when blank —
# a config pointing at another gateway keeps its own URL.
if not str(cfg.get("llm_base_url", "")).strip():
    cfg["llm_base_url"] = "https://campaign-api.autonomous.ai/api/v1/ai/v1"
    print("[os-dev-seed] llm_base_url was empty — defaulted to https://campaign-api.autonomous.ai/api/v1/ai/v1")
if not str(cfg.get("llm_model", "")).strip():
    cfg["llm_model"] = "Auto-AI"
    print("[os-dev-seed] llm_model was empty — defaulted to Auto-AI")
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print(f"[os-dev-seed] {path}: device_type={device_type} agent_runtime={runtime} set_up_completed=true")

# Name what each missing credential costs, at the moment the dev can act on it.
# A silent empty key surfaces much later as "the device never speaks" or a turn
# that answers blind, and neither points back here.
missing = []
if not str(cfg.get("llm_api_key", "")).strip():
    missing.append("llm_api_key — no TTS (silent replies), no STT, no Gemini Live, "
                   "no image description, and the web UI redirects to /setup "
                   f"(adminAuthMiddleware answers 503 without it). Fill it in: nano {path}")
for m in missing:
    print(f"[os-dev-seed] NOTE: {m}")
PY

# presync.sh regenerates config.toml from config.json on every boot and keeps
# only [mcp_servers.*]. Back up a hand-written one once so pointing CODEX_HOME
# at a real install is not a one-way door.
if [ "$AGENT_RUNTIME" = "codex" ] && [ -f "$CODEX_HOME/config.toml" ] && [ ! -f "$CODEX_HOME/config.toml.pre-os-dev" ]; then
  cp "$CODEX_HOME/config.toml" "$CODEX_HOME/config.toml.pre-os-dev"
  log "backed up $CODEX_HOME/config.toml → config.toml.pre-os-dev (presync rewrites it)"
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
