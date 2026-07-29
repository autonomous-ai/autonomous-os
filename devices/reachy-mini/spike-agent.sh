#!/usr/bin/env bash
# spike-agent.sh — Install the OpenClaw gateway on a Reachy Mini.
#
# Runs FROM YOUR MAC. Fourth spike script: spike-hal.sh (body), spike-os.sh
# (API), spike-web.sh (browser), this one (brain). Mirrors stage_openclaw in
# scripts/provision/setup.sh, minus the parts Reachy does not need.
#
# Why it matters beyond chat: os-server's status reporter skips every tick until
# the agent gateway is ready (system/device/status_reporter.go), so the backend
# never assigns a device_id and MQTT stays subscribed to an empty channel. No
# gateway means no cloud identity.
#
# Deltas from the lamp install, deliberate:
#   - no chromium / xvfb: they exist for the computer-use skill, which is gated
#     on the `companion` capability that Reachy's DEVICE.md does not declare.
#     That also keeps ~600 MB off a 14 GB eMMC that is already 80% full.
#   - no skills seeding: os-server re-syncs and prunes skills on boot.
#
# Usage:
#   bash devices/reachy-mini/spike-agent.sh          # install + start
#   bash devices/reachy-mini/spike-agent.sh --stop   # stop and disable the unit
set -euo pipefail

REACHY_HOST="${REACHY_HOST:-pollen@reachy-mini.local}"
OPENCLAW_VERSION="${OPENCLAW_VERSION:-2026.6.10}"
OPENCLAW_HOME="/root/.openclaw"

STOP_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --stop) STOP_ONLY=1 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n========== %s ==========\n' "$1"; }

if [ "$STOP_ONLY" = "1" ]; then
  say "Stopping OpenClaw"
  ssh "$REACHY_HOST" "sudo systemctl disable --now openclaw.service 2>/dev/null || true; echo stopped"
  exit 0
fi

echo "target: $REACHY_HOST   openclaw: $OPENCLAW_VERSION"

say "1/4  Install node + npm"
ssh "$REACHY_HOST" bash <<'REMOTE_NODE'
set -e
avail_kb=$(df -Pk / | awk 'NR==2 {print $4}')
echo "[spike-agent] free space: $(awk -v k="$avail_kb" 'BEGIN {printf "%.1f", k/1048576}') GB"
if [ "$avail_kb" -lt 1572864 ]; then
  echo "[spike-agent] ABORT: need at least 1.5 GB free for node + the openclaw package."
  exit 1
fi

# NodeSource, not apt: Debian trixie ships node 20, and openclaw hard-refuses to
# start on anything below 22.19 ("Node.js v22.19+ is required") — npm only WARNs
# at install time, so the failure surfaces as a systemd crash-loop instead.
# Same source and major as the lamp (scripts/provision/setup.sh, build-orangepi.sh).
need_node=1
if command -v node >/dev/null 2>&1; then
  major=$(node --version | sed 's/^v//; s/\..*//')
  [ "$major" -ge 22 ] && need_node=0
fi

if [ "$need_node" = "0" ]; then
  echo "[spike-agent] node $(node --version), npm $(npm --version) already present"
else
  echo "[spike-agent] installing Node.js 22 (NodeSource)..."
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
  sudo apt-get install -y nodejs
  echo "[spike-agent] installed node $(node --version), npm $(npm --version)"
fi
REMOTE_NODE

say "2/4  Install openclaw@$OPENCLAW_VERSION"
# Pinned deliberately: 2026.6.11 carries a P1 regression (#98528). The lamp runs
# the same pin, so keeping them equal keeps bug reports comparable.
# The browser-download opt-outs matter at INSTALL time too, not just at runtime:
# without them npm pulls a Chromium build we deleted the use case for.
ssh "$REACHY_HOST" bash <<REMOTE_OPENCLAW
set -e
if openclaw --version 2>/dev/null | grep -q "$OPENCLAW_VERSION"; then
  echo "[spike-agent] openclaw $OPENCLAW_VERSION already installed"
else
  sudo env PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 PUPPETEER_SKIP_DOWNLOAD=1 \
    npm install -g --no-fund --no-audit openclaw@$OPENCLAW_VERSION
fi
echo -n "[spike-agent] openclaw version: "; openclaw --version || echo "(version check failed)"
REMOTE_OPENCLAW

say "3/4  Seed $OPENCLAW_HOME"
ssh "$REACHY_HOST" bash <<REMOTE_SEED
set -e
HOME_DIR="$OPENCLAW_HOME"
sudo mkdir -p \\
  "\$HOME_DIR" "\$HOME_DIR/workspace" \\
  "\$HOME_DIR/agents/main/agent" "\$HOME_DIR/agents/main/sessions" \\
  "\$HOME_DIR/credentials" "\$HOME_DIR/.cache" "\$HOME_DIR/.config" \\
  "\$HOME_DIR/.local/share" /var/log/openclaw
for p in "\$HOME_DIR" "\$HOME_DIR/workspace" "\$HOME_DIR/agents" "\$HOME_DIR/agents/main" \\
         "\$HOME_DIR/agents/main/agent" "\$HOME_DIR/agents/main/sessions" \\
         "\$HOME_DIR/credentials" "\$HOME_DIR/.cache" "\$HOME_DIR/.config" \\
         "\$HOME_DIR/.local" "\$HOME_DIR/.local/share"; do
  sudo chmod 700 "\$p" 2>/dev/null || true
done

# Never regenerate the token on an existing install: os-server caches it, and a
# mismatch closes the gateway WS with 1008 / token_mismatch.
if sudo test -f "\$HOME_DIR/openclaw.json"; then
  echo "[spike-agent] openclaw.json already present — keeping it (token preserved)"
else
  TOKEN="\$(openssl rand -hex 24 2>/dev/null || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  sudo tee "\$HOME_DIR/openclaw.json" >/dev/null <<JSON
{
  "agents": {
    "defaults": {
      "workspace": "\$HOME_DIR/workspace"
    }
  },
  "gateway": {
    "mode": "local",
    "bind": "loopback",
    "port": 18789,
    "auth": {
      "mode": "token",
      "token": "\$TOKEN"
    },
    "controlUi": {
      "allowedOrigins": ["http://127.0.0.1", "http://localhost"],
      "allowInsecureAuth": false
    }
  }
}
JSON
  sudo chmod 600 "\$HOME_DIR/openclaw.json"
  echo "[spike-agent] seeded openclaw.json with a fresh gateway token"
fi
REMOTE_SEED

say "4/4  systemd unit + start"
# The gateway was the first spike to need a unit rather than tmux: os-server
# restarts it with `systemctl restart openclaw.service` after onboarding writes
# config, so a tmux-run gateway would be un-restartable by the very service that
# drives it. spike-hal.sh and spike-os.sh install units of their own now too.
ssh "$REACHY_HOST" bash <<REMOTE_UNIT
set -e
OPENCLAW_BIN=\$(command -v openclaw)
[ -n "\$OPENCLAW_BIN" ] || { echo "ERROR: openclaw not on PATH after install"; exit 1; }

sudo tee /etc/systemd/system/openclaw.service >/dev/null <<UNIT
[Unit]
Description=OpenClaw Gateway
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$OPENCLAW_HOME
Environment="OPENCLAW_HOME=$OPENCLAW_HOME"
Environment="OPENCLAW_STATE_DIR=$OPENCLAW_HOME"
Environment="HOME=/root"
Environment="XDG_CACHE_HOME=$OPENCLAW_HOME/.cache"
Environment="XDG_CONFIG_HOME=$OPENCLAW_HOME/.config"
Environment="XDG_DATA_HOME=$OPENCLAW_HOME/.local/share"
Environment="PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1"
LimitNOFILE=65535
MemoryMax=1500M
ExecStart=\$OPENCLAW_BIN gateway run
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now openclaw.service
sleep 5
echo "[spike-agent] unit state: \$(systemctl is-active openclaw.service)"
REMOTE_UNIT

cat <<EOF

========================================
  OpenClaw $OPENCLAW_VERSION installed on $REACHY_HOST
    logs  : ssh $REACHY_HOST 'sudo journalctl -u openclaw -f'
    port  : 18789 (loopback) — os-server dials ws://127.0.0.1:18789
    stop  : bash devices/reachy-mini/spike-agent.sh --stop
========================================

Once the gateway reports ready, os-server's status reporter starts pinging and
the backend assigns device_id + the MQTT fa/fd channels.
EOF
