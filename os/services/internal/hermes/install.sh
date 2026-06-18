#!/usr/bin/env bash
# runtimes/hermes/install.sh — installer for the Hermes agentic backend.
#
# Published to the CDN at ${RUNTIMES_BASE_URL}/hermes/install.sh and fetched by
# /usr/local/bin/switch-runtime the first time a device switches to hermes. It
# is self-contained: nothing in the imager or os-server knows about hermes.
#
# Contract every backend installer must satisfy (see switch_runtime.sh):
#   1. create a  <name>.service  systemd unit (here: hermes.service), NOT enabled
#      — switch-runtime brings it up;
#   2. optionally drop  /usr/local/bin/runtime-<name>-presync  for per-switch
#      config sync (here: sync llm_* from config.json into the Hermes config,
#      since `hermes claw migrate` does NOT carry model config across).
#
# Reference: hermes_setup (2).sh (repo root).
#
# ⚠️ VERIFY ON DEVICE: `hermes gateway` must listen on 127.0.0.1:8642 to match
#    internal/hermes/constants.go BaseURL. If its default port differs, add the
#    port flag/env to the ExecStart below.
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

echo "[install-hermes] migrate skills from OpenClaw (model config is synced separately)"
"$HERMES_BIN" claw migrate --preset full --overwrite --skill-conflict rename --yes \
  || echo "[install-hermes] WARN: claw migrate failed (non-fatal — no OpenClaw state yet?)"

mkdir -p "$HERMES_DIR" /var/log/hermes
touch "$ENV_FILE"

echo "[install-hermes] seed $ENV_FILE"
for k in API_SERVER_ENABLED API_SERVER_KEY API_SERVER_CORS_ORIGINS TELEGRAM_ALLOWED_USERS; do
  sed -i "/^${k}=/d" "$ENV_FILE"
done
cat >>"$ENV_FILE" <<EOF
API_SERVER_ENABLED=true
API_SERVER_KEY=${HERMES_API_SERVER_KEY}
API_SERVER_CORS_ORIGINS=http://localhost:3000
TELEGRAM_ALLOWED_USERS=y
EOF
# AUTONOMOUS_API_KEY (the LLM provider key) + model + base_url are written by the
# presync hook from config.json on every switch, so the device's real values win.

echo "[install-hermes] seed $CONFIG_YAML (model/base_url overwritten by presync)"
cat >"$CONFIG_YAML" <<'EOF'
model:
  default: gpt-5.5
  provider: custom:autonomous
custom_providers:
  - name: autonomous
    base_url: https://campaign-api.autonomous.ai/api/v1/ai
    key_env: AUTONOMOUS_API_KEY
    api_mode: anthropic_messages
EOF

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

LLM_MODEL="$(jq -r '.llm_model    // empty' "$CONFIG_JSON" 2>/dev/null || true)"
LLM_BASE_URL="$(jq -r '.llm_base_url // empty' "$CONFIG_JSON" 2>/dev/null || true)"
LLM_API_KEY="$(jq -r '.llm_api_key  // empty' "$CONFIG_JSON" 2>/dev/null || true)"

if [ -n "$LLM_MODEL" ]; then
  yq -i ".model.default = \"$LLM_MODEL\"" "$CONFIG_YAML"
  log "model.default = $LLM_MODEL"
fi
if [ -n "$LLM_BASE_URL" ]; then
  yq -i ".custom_providers[0].base_url = \"$LLM_BASE_URL\"" "$CONFIG_YAML"
  log "custom_providers[0].base_url = $LLM_BASE_URL"
fi
if [ -n "$LLM_API_KEY" ]; then
  sed -i '/^AUTONOMOUS_API_KEY=/d' "$ENV_FILE"
  echo "AUTONOMOUS_API_KEY=$LLM_API_KEY" >>"$ENV_FILE"
  log "AUTONOMOUS_API_KEY synced"
fi
PRESYNC
chmod +x /usr/local/bin/runtime-hermes-presync

echo "[install-hermes] create hermes.service (NOT enabled — switch-runtime starts it)"
cat >/etc/systemd/system/hermes.service <<EOF
[Unit]
Description=Hermes Agent Backend
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$HERMES_DIR
Environment="HOME=/root"
LimitNOFILE=65535
MemoryMax=1500M
# ⚠️ Must listen on 127.0.0.1:8642 (internal/hermes/constants.go BaseURL).
ExecStart=$HERMES_BIN gateway
Restart=always
RestartSec=5
StandardOutput=append:/var/log/hermes/output.log
StandardError=append:/var/log/hermes/error.log

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload

echo "[install-hermes] done — hermes.service created (inactive). switch-runtime will start it."
