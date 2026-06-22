#!/usr/bin/env bash
# runtimes/hermes/install.sh — installer for the Hermes agentic backend.
#
# Published to the CDN at ${RUNTIMES_BASE_URL}/hermes/install.sh and fetched by
# /usr/local/bin/switch-runtime the first time a device switches to hermes. It
# is self-contained: nothing in the imager or os-server knows about hermes.
#
# This installer is self-sufficient: a direct `bash install.sh` fully configures
# AND starts Hermes — it no longer relies on switch-runtime to run the presync
# hook or to enable the unit afterwards. It still also drops the presync hook so
# later runtime switches re-sync the latest llm_* from config.json.
#
# What it does:
#   1. install the Hermes CLI, stop openclaw, then migrate skills from OpenClaw;
#   2. seed .env, then patch ONLY .model + .custom_providers in config.yaml
#      (yq, not a full-file overwrite), then run the presync hook once to pull
#      the device's real llm_model / llm_base_url / llm_api_key from config.json;
#   3. drop /usr/local/bin/runtime-hermes-presync for per-switch config sync
#      (since `hermes claw migrate` does NOT carry model config across);
#   4. install + start the gateway as a system service via
#      `hermes gateway install/start --system` (unit: hermes-gateway.service).
#
# Reference: hermes_setup.sh (~/Downloads) for the yq config patch + .env seeding.
#
# UNIT NAME: the gateway runs as hermes-gateway.service; we declare it in
#    /usr/local/lib/os-runtimes/hermes/service so switch_runtime.sh enables the
#    right unit.
#
# ⚠️ VERIFY ON DEVICE: `hermes gateway` must listen on 127.0.0.1:8642 to match
#    internal/hermes/constants.go BaseURL.
set -euo pipefail

HERMES_BIN="/usr/local/bin/hermes"
HERMES_DIR="/root/.hermes"
ENV_FILE="$HERMES_DIR/.env"
CONFIG_YAML="$HERMES_DIR/config.yaml"

# API_SERVER_KEY MUST equal internal/hermes/constants.go APIKey — os-server sends
# it as the Bearer on every /v1/responses call. Mismatch ⇒ 401 on every turn.
HERMES_API_SERVER_KEY="hermes-api-key"

echo "[install-hermes] prerequisites (jq, yq, curl)"
apt-get update || true
apt-get install -y jq curl || true
if ! command -v yq >/dev/null 2>&1; then
  case "$(uname -m)" in
    x86_64)        YQ_BIN="yq_linux_amd64" ;;
    aarch64|arm64) YQ_BIN="yq_linux_arm64" ;;
    armv7l|armv6l) YQ_BIN="yq_linux_arm" ;;
    *) echo "[install-hermes] ERROR: unsupported arch $(uname -m) for yq"; exit 1 ;;
  esac
  curl -fsSL "https://github.com/mikefarah/yq/releases/download/v4.46.1/${YQ_BIN}" -o /usr/local/bin/yq
  chmod +x /usr/local/bin/yq
fi

echo "[install-hermes] install Hermes CLI"
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --skip-setup
if [ ! -x "$HERMES_BIN" ]; then
  echo "[install-hermes] ERROR: hermes not found at $HERMES_BIN after install" >&2
  command -v hermes || true
  exit 1
fi
"$HERMES_BIN" --version || true

echo "[install-hermes] stop openclaw before migrating (avoid racing its state)"
# Retry the stop up to 3 times, verifying openclaw actually went inactive between
# tries. After 3 attempts proceed regardless so a stuck openclaw never blocks the
# install — claw migrate is non-fatal anyway.
for attempt in 1 2 3; do
  systemctl stop openclaw 2>/dev/null || true
  if ! systemctl is-active --quiet openclaw; then
    echo "[install-hermes] openclaw stopped (attempt ${attempt}/3)"
    break
  fi
  echo "[install-hermes] WARN: openclaw still active after attempt ${attempt}/3"
  [ "$attempt" -eq 3 ] && echo "[install-hermes] WARN: openclaw still active after 3 attempts — continuing anyway"
  sleep 1
done

echo "[install-hermes] migrate skills from OpenClaw (model config is synced separately)"
"$HERMES_BIN" claw migrate --preset full --overwrite --skill-conflict rename --yes --migrate-secrets \
  || echo "[install-hermes] WARN: claw migrate failed (non-fatal — no OpenClaw state yet?)"

mkdir -p "$HERMES_DIR" /var/log/hermes
touch "$ENV_FILE"

echo "[install-hermes] seed $ENV_FILE (upsert — other vars preserved)"
for k in API_SERVER_ENABLED API_SERVER_KEY API_SERVER_CORS_ORIGINS; do
  sed -i "/^${k}=/d" "$ENV_FILE"
done

[ -s "$ENV_FILE" ] && [ -n "$(tail -c1 "$ENV_FILE")" ] && printf '\n' >>"$ENV_FILE"
cat >>"$ENV_FILE" <<EOF
API_SERVER_ENABLED=true
API_SERVER_KEY=${HERMES_API_SERVER_KEY}
API_SERVER_CORS_ORIGINS=http://localhost:3000
EOF
# AUTONOMOUS_API_KEY (LLM key) + model + base_url + TELEGRAM_ALLOWED_USERS are
# written by the presync hook from config.json on every switch, so the device's
# real values win.

echo "[install-hermes] patch $CONFIG_YAML — only .model + .custom_providers (presync overwrites model.default/base_url from config.json)"
# Patch in place: replace ONLY the two keys we own, preserving anything else the
# Hermes CLI wrote to config.yaml (do not clobber the whole file). touch first so
# yq has a doc to edit on a fresh standalone install where config.yaml is absent.
touch "$CONFIG_YAML"
yq -i '
  .model = {"default": "gpt-5.5", "provider": "custom:autonomous"}
  |
  .custom_providers = [
    {
      "name": "autonomous",
      "base_url": "https://campaign-api.autonomous.ai/api/v1/ai",
      "key_env": "AUTONOMOUS_API_KEY",
      "api_mode": "anthropic_messages"
    }
  ]
' "$CONFIG_YAML"

echo "[install-hermes] drop /usr/local/bin/runtime-hermes-presync (llm_* sync hook)"
cat >/usr/local/bin/runtime-hermes-presync <<'PRESYNC'
#!/usr/bin/env bash
# runtime-hermes-presync — run by switch-runtime right before hermes starts.
# Syncs llm_* from config.json into the Hermes config; `hermes claw migrate`
# does NOT carry model config across, so without this Hermes runs the wrong
# model/key/endpoint vs the device's configured AI brain.
set -euo pipefail
CONFIG_JSON="/root/config/config.json"
HERMES_DIR="/root/.hermes"
ENV_FILE="$HERMES_DIR/.env"
CONFIG_YAML="$HERMES_DIR/config.yaml"
log() { echo "[hermes-presync] $*"; }

# config.yaml model/provider come from llm_* (structured edits via yq).
LLM_MODEL="$(jq -r '.llm_model    // empty' "$CONFIG_JSON" 2>/dev/null || true)"
LLM_BASE_URL="$(jq -r '.llm_base_url // empty' "$CONFIG_JSON" 2>/dev/null || true)"
if [ -n "$LLM_MODEL" ]; then
  yq -i ".model.default = \"$LLM_MODEL\"" "$CONFIG_YAML"
  log "model.default = $LLM_MODEL"
fi
if [ -n "$LLM_BASE_URL" ]; then
  yq -i ".custom_providers[0].base_url = \"$LLM_BASE_URL\"" "$CONFIG_YAML"
  log "custom_providers[0].base_url = $LLM_BASE_URL"
fi

# .env secrets/IDs: config.json wins — UPSERT each non-empty config.json field
# into its Hermes env var (replace the existing line, or append if absent). Other
# variables are never touched, so whatever `hermes claw migrate` carried over
# from OpenClaw is kept as the fallback. Empty/missing config fields are skipped.
# (Bot tokens like TELEGRAM_BOT_TOKEN usually arrive via migrate, but we still
# sync them in case OpenClaw had none.)
sync_env() {
  local key="$1" var="$2" val
  val="$(jq -r ".${key} // empty" "$CONFIG_JSON" 2>/dev/null || true)"
  [ -n "$val" ] || return 0
  sed -i "/^${var}=/d" "$ENV_FILE"
  # guard against gluing onto a previous line that lacks a trailing newline
  [ -s "$ENV_FILE" ] && [ -n "$(tail -c1 "$ENV_FILE")" ] && printf '\n' >>"$ENV_FILE"
  echo "${var}=${val}" >>"$ENV_FILE"
  log "${var} synced"
}

sync_env llm_api_key        AUTONOMOUS_API_KEY
sync_env telegram_bot_token TELEGRAM_BOT_TOKEN
sync_env telegram_user_id   TELEGRAM_ALLOWED_USERS
sync_env slack_bot_token    SLACK_BOT_TOKEN
sync_env slack_app_token    SLACK_APP_TOKEN
sync_env slack_user_id      SLACK_ALLOWED_USERS
sync_env discord_bot_token  DISCORD_BOT_TOKEN
sync_env discord_guild_id   DISCORD_GUILD_ID
sync_env discord_user_id    DISCORD_ALLOWED_USERS
sync_env whatsapp_user_id   WHATSAPP_ALLOWED_USERS
PRESYNC
chmod +x /usr/local/bin/runtime-hermes-presync

# Run the hook once now so a direct `bash install.sh` is fully configured (.env
# AUTONOMOUS_API_KEY + config.yaml model/base_url synced from config.json),
# instead of only on the next switch-runtime call. Later switches still re-run
# this same hook to pick up the latest llm_* values.
echo "[install-hermes] sync llm_* from config.json now (via runtime-hermes-presync)"
/usr/local/bin/runtime-hermes-presync \
  || echo "[install-hermes] WARN: presync failed (config.json missing? non-fatal — switch-runtime retries on next switch)"

echo "[install-hermes] install + start hermes gateway as a system service"
yes y | "$HERMES_BIN" gateway install --system --run-as-user root
"$HERMES_BIN" gateway start --system
"$HERMES_BIN" gateway status --system || true

# Declare our unit name (hermes-gateway, not the default <runtime>.service) so
# switch-runtime enables/disables the right unit. Best-effort: the dir exists
# when os-server materialized this installer; create it for standalone runs too.
echo "[install-hermes] declare unit name for switch-runtime (hermes-gateway)"
mkdir -p /usr/local/lib/os-runtimes/hermes
echo "hermes-gateway" >/usr/local/lib/os-runtimes/hermes/service

echo "[install-hermes] done — hermes gateway installed + started as a system service (hermes-gateway.service)."
