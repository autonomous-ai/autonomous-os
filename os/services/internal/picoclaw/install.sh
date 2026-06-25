#!/usr/bin/env bash
# runtimes/picoclaw/install.sh — installer for the PicoClaw agentic backend.
#
# Published to the CDN at ${RUNTIMES_BASE_URL}/picoclaw/install.sh and fetched by
# /usr/local/bin/switch-runtime the first time a device switches to picoclaw. It
# is self-contained: nothing in the imager or os-server knows about picoclaw.
#
# This installer is self-sufficient: a direct `bash install.sh` fully configures
# AND starts PicoClaw — it does not rely on switch-runtime to run the presync hook
# or to enable the unit afterwards. It still drops the presync hook so later
# runtime switches re-sync the latest model/channel config from config.json.
#
# What it does:
#   1. install the PicoClaw binary (pinned release, arm64) to /usr/local/bin;
#   2. `picoclaw onboard` to create /root/.picoclaw (workspace + a baseline
#      config.json + .security.yml the presync hook then patches);
#   3. install + start the gateway as a SYSTEM service. `picoclaw gateway` only
#      runs in the foreground, so unlike hermes (which ships its own
#      `gateway install --system`) we write the systemd unit ourselves — the
#      default unit name picoclaw.service matches the runtime name, so
#      switch-runtime needs NO /usr/local/lib/os-runtimes/picoclaw/service file;
#   4. drop /usr/local/bin/runtime-picoclaw-presync ownership to os-server and run
#      it once: it OWNS the model wiring (config.json agents.defaults + model_list,
#      .security.yml api_keys) AND the channel wiring (config.json channel_list +
#      .security.yml channel tokens), and — when the .openclaw-migrated marker is
#      absent (first install OR after a factory reset wiped /root/.picoclaw) — runs
#      `picoclaw migrate --workspace-only --force` to carry persona/memory/skills over from OpenClaw.
#      See presync.sh.
#
# UNIT NAME: picoclaw.service (== runtime name). No service-name declaration file
#    is needed (switch_runtime.sh defaults the unit to the runtime name).
#
# ⚠️ VERIFY ON DEVICE: `picoclaw gateway` must listen on 127.0.0.1:18790 and serve
#    the WebSocket at /pico/ws/ to match internal/picoclaw/constants.go WSURL, and
#    the bearer token must equal constants.go Token (seeded into .security.yml
#    channel_list.pico.settings.token by the presync hook).
set -euo pipefail

# Tee all output to a log under /root/.picoclaw (persistent rootfs), NOT /var/log
# — on these boards /var/log is a volatile zram mount wiped on reboot, which would
# lose the install log exactly when you need it. Override with PICO_LOG=... if needed.
PICO_LOG="${PICO_LOG:-/root/.picoclaw/install.log}"
mkdir -p "$(dirname "$PICO_LOG")"
exec > >(tee -a "$PICO_LOG") 2>&1
echo "[install-picoclaw] ===== install start $(date -u '+%Y-%m-%dT%H:%M:%SZ') (log: $PICO_LOG) ====="

PICO_BIN="/usr/local/bin/picoclaw"
PICO_DIR="/root/.picoclaw"
PICO_CONFIG="$PICO_DIR/config.json"

# Pin the release the device installs. Bump here (and re-OTA os-server) to upgrade.
# The asset is published per-arch as picoclaw-linux-<arch> on GitHub releases.
PICO_VERSION="${PICO_VERSION:-v0.2.9-toolfix}"
PICO_REPO="${PICO_REPO:-autonomous-ai/picoclaw}"

echo "[install-picoclaw] prerequisites (jq, yq, curl)"
apt-get update || true
apt-get install -y jq curl || true
# yq is required by the presync hook's .security.yml (YAML) edits. install it the
# same way the hermes installer does (static binary per arch).
if ! command -v yq >/dev/null 2>&1; then
  case "$(uname -m)" in
    x86_64)        YQ_BIN="yq_linux_amd64" ;;
    aarch64|arm64) YQ_BIN="yq_linux_arm64" ;;
    armv7l|armv6l) YQ_BIN="yq_linux_arm" ;;
    *) echo "[install-picoclaw] ERROR: unsupported arch $(uname -m) for yq"; exit 1 ;;
  esac
  curl -fsSL "https://github.com/mikefarah/yq/releases/download/v4.46.1/${YQ_BIN}" -o /usr/local/bin/yq
  chmod +x /usr/local/bin/yq
fi

echo "[install-picoclaw] install PicoClaw binary ${PICO_VERSION}"
case "$(uname -m)" in
  aarch64|arm64) PICO_ASSET="picoclaw-linux-arm64" ;;
  x86_64)        PICO_ASSET="picoclaw-linux-amd64" ;;
  *) echo "[install-picoclaw] ERROR: unsupported arch $(uname -m) for picoclaw"; exit 1 ;;
esac
PICO_URL="https://github.com/${PICO_REPO}/releases/download/${PICO_VERSION}/${PICO_ASSET}"
PICO_TMP="$(mktemp)"
echo "[install-picoclaw] downloading ${PICO_URL}"
curl -fsSL "$PICO_URL" -o "$PICO_TMP"
install -m 0755 "$PICO_TMP" "$PICO_BIN"
rm -f "$PICO_TMP"
if [ ! -x "$PICO_BIN" ]; then
  echo "[install-picoclaw] ERROR: picoclaw not found at $PICO_BIN after install" >&2
  exit 1
fi
"$PICO_BIN" --version || true

# onboard creates /root/.picoclaw itself (workspace + a baseline config.json /
# .security.yml) — no explicit mkdir needed. It is non-interactive. Run it only
# when there is no config yet, so a reinstall over an existing (possibly
# migrated/customized) install does not reset the baseline — the presync hook owns
# keeping config.json/.security.yml correct.
echo "[install-picoclaw] onboard (create workspace + baseline config) if absent"
if [ ! -f "$PICO_CONFIG" ]; then
  HOME=/root "$PICO_BIN" onboard || {
    echo "[install-picoclaw] ERROR: picoclaw onboard failed" >&2
    exit 1
  }
else
  echo "[install-picoclaw] config.json already present — skipping onboard"
fi

# OpenClaw persona/memory/skill import (`picoclaw migrate --workspace-only --force`) is owned by the
# presync hook now — it runs the migrate when the .openclaw-migrated marker is absent
# (first install OR after a factory reset wiped /root/.picoclaw), then asserts the
# model/channel config on top. Owning it there (not here) is what lets a plain
# os-server OTA refresh the logic: this installer only re-runs on a first install /
# failed verify. See presync.sh §0. (openclaw is stopped by the presync hook right
# before migrate to avoid racing its on-disk state.)

# Model + channel wiring (config.json agents.defaults/model_list/channel_list and
# .security.yml api_keys/channel tokens) is owned ENTIRELY by the presync hook, NOT
# patched here. The hook is materialized to /usr/local/bin/runtime-picoclaw-presync
# by os-server BEFORE this installer runs. Running it now configures this fresh
# install; switch-runtime re-runs it before every later start, so the config
# self-heals (e.g. after a factory reset reset the baseline).
PRESYNC_HOOK="/usr/local/bin/runtime-picoclaw-presync"
if [ -x "$PRESYNC_HOOK" ]; then
  echo "[install-picoclaw] migrate + patch model/channel config now (via $PRESYNC_HOOK)"
  "$PRESYNC_HOOK" \
    || echo "[install-picoclaw] WARN: presync failed (config.json missing? non-fatal — switch-runtime retries on next switch)"
else
  echo "[install-picoclaw] WARN: $PRESYNC_HOOK absent — os-server did not materialize it (standalone/offline run?); PicoClaw model/channel config NOT set"
fi

# `picoclaw gateway` runs in the foreground only, so wrap it in a systemd unit so
# switch-runtime can enable/disable/verify it like any other backend. Unit name ==
# runtime name (picoclaw.service), so no service-name declaration file is needed.
# HOME=/root so the gateway resolves its data dir at /root/.picoclaw.
echo "[install-picoclaw] write systemd unit picoclaw.service"
cat >/etc/systemd/system/picoclaw.service <<UNIT
[Unit]
Description=PicoClaw agent gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Environment=HOME=/root
WorkingDirectory=/root/.picoclaw
ExecStart=/usr/local/bin/picoclaw gateway
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
echo "[install-picoclaw] enable + start picoclaw.service"
systemctl enable --now picoclaw.service
systemctl status picoclaw.service --no-pager || true

# Drop a verify hook so switch-runtime can distinguish a real install from an
# orphaned picoclaw.service whose binary is gone/broken — when it fails,
# switch-runtime reinstalls instead of skipping. Keep it cheap + offline.
echo "[install-picoclaw] declare verify hook for switch-runtime (command -v picoclaw)"
mkdir -p /usr/local/lib/os-runtimes/picoclaw
cat >/usr/local/lib/os-runtimes/picoclaw/verify <<'VERIFY'
#!/usr/bin/env bash
command -v picoclaw >/dev/null 2>&1
VERIFY
chmod +x /usr/local/lib/os-runtimes/picoclaw/verify

echo "[install-picoclaw] done — picoclaw gateway installed + started (picoclaw.service)."
echo "[install-picoclaw] ===== install finished $(date -u '+%Y-%m-%dT%H:%M:%SZ') ====="
