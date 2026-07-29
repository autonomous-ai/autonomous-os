#!/usr/bin/env bash
# spike-os.sh — Deploy and run ONLY os-server on a Reachy Mini.
#
# Runs FROM YOUR MAC. Cross-compiles the Go binary, rsyncs it, seeds a minimal
# config, and runs it under a systemd unit. Companion to spike-hal.sh (body) —
# this is the API/agent layer.
#
# Why a unit and not tmux: a tmux session dies with the SSH connection and never
# comes back after a power cut. Neither script is production even so — the units
# point at the spike layout (/opt/autonomous), not the OTA layout
# (/usr/local/bin/os-server) that scripts/provision/setup.sh installs, and there
# is still no nginx or OTA here.
#
# What this does NOT give you: the web UI. os-server binds 127.0.0.1:5000 and
# serves no static files — nginx is what serves the dist and proxies /api
# (scripts/provision/setup.sh). Until that exists for Reachy, reach the API from
# the Pi itself, or tunnel it:
#
#   ssh -L 5000:localhost:5000 pollen@reachy-mini.local
#   # then on the Mac: curl localhost:5000/api/health/live
#
# Why root + cwd=/root: config.Load reads the RELATIVE path "config/config.json"
# (system/server/config/config.go), so production's WorkingDirectory=/root makes
# it /root/config/config.json — the same file HAL looks for via OS_CONFIG_PATH.
# Running from anywhere else silently splits the two services onto different
# configs. The logger also writes /var/log/os-server.log, which needs root.
#
# Usage:
#   bash devices/reachy-mini/spike-os.sh              # build + deploy + run
#   bash devices/reachy-mini/spike-os.sh --no-build   # redeploy the existing binary
#   bash devices/reachy-mini/spike-os.sh --stop       # stop it
#   bash devices/reachy-mini/spike-os.sh --uninstall  # stop + remove the unit
set -euo pipefail

REACHY_HOST="${REACHY_HOST:-pollen@reachy-mini.local}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REMOTE_BASE="/opt/autonomous"
SERVICE="os-server"

SKIP_BUILD=0
STOP_ONLY=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --no-build)  SKIP_BUILD=1 ;;
    --stop)      STOP_ONLY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n========== %s ==========\n' "$1"; }

if [ "$STOP_ONLY" = "1" ] || [ "$UNINSTALL" = "1" ]; then
  say "Stopping os-server on $REACHY_HOST"
  ssh "$REACHY_HOST" bash <<REMOTE_STOP
sudo systemctl stop $SERVICE 2>/dev/null || true
# Legacy path: earlier versions of this script ran os-server in tmux.
tmux kill-session -t os 2>/dev/null || true
sudo pkill -f '$REMOTE_BASE/os-server' 2>/dev/null || true
if [ "$UNINSTALL" = "1" ]; then
  sudo systemctl disable $SERVICE 2>/dev/null || true
  sudo rm -f /etc/systemd/system/$SERVICE.service
  sudo systemctl daemon-reload
  echo "[spike-os] unit removed"
fi
echo stopped
REMOTE_STOP
  [ "$UNINSTALL" = "1" ] || echo "Still enabled — it returns on the next boot (--uninstall to prevent that)."
  exit 0
fi

echo "target: $REACHY_HOST"

if [ "$SKIP_BUILD" = "0" ]; then
  say "1/4  Build os-server (linux/arm64)"
  cd "$ROOT_DIR"
  make os-build
else
  say "1/4  Build skipped (--no-build)"
  [ -f "$ROOT_DIR/system/os-server" ] || { echo "no binary at system/os-server — drop --no-build"; exit 1; }
fi

say "2/4  Copy binary"
ssh "$REACHY_HOST" "sudo mkdir -p $REMOTE_BASE && sudo chown \$(id -u):\$(id -g) $REMOTE_BASE"
# Replacing a running binary in place gives ETXTBSY; stop first, then copy.
# tmux is the legacy path — earlier versions of this script ran it there, and a
# leftover session would hold port 5000 and make the unit crash-loop.
ssh "$REACHY_HOST" "sudo systemctl stop $SERVICE 2>/dev/null || true; tmux kill-session -t os 2>/dev/null || true; sudo pkill -f '$REMOTE_BASE/os-server' 2>/dev/null || true; sleep 1" || true
scp "$ROOT_DIR/system/os-server" "$REACHY_HOST:$REMOTE_BASE/os-server"
ssh "$REACHY_HOST" "chmod +x $REMOTE_BASE/os-server"

say "3/4  Seed /root/config/config.json"
# Minimal config: the only fail-loud startup guard is device_type
# (server.go: "device_type unresolved ... refusing to assume 'lamp'"). Everything
# else defaults, and the web setup flow fills the rest in. Never overwrite an
# existing config — it holds provisioning state.
ssh "$REACHY_HOST" bash <<'REMOTE_CONF'
set -e
if sudo test -f /root/config/config.json; then
  echo "[spike-os] /root/config/config.json already present — keeping it"
else
  sudo mkdir -p /root/config
  sudo tee /root/config/config.json >/dev/null <<'JSON'
{
  "httpPort": 5000,
  "device_type": "reachy-mini"
}
JSON
  echo "[spike-os] seeded /root/config/config.json"
fi
REMOTE_CONF

say "4/4  Install the systemd unit and start os-server"
# One safety check before enabling anything at boot: os-server calls
# SwitchToAPMode() at startup whenever set_up_completed is false
# (system/server/config_watch.go). On a provisioned image that tears down the
# WiFi station — over SSH that means losing the robot until someone plugs in a
# keyboard. Refuse rather than find out after the reboot.
ssh "$REACHY_HOST" bash <<'REMOTE_APCHECK'
set -e
if sudo grep -q '"set_up_completed"[[:space:]]*:[[:space:]]*true' /root/config/config.json 2>/dev/null; then
  echo "[spike-os] set_up_completed=true — no AP switch at boot"
elif [ -x /usr/local/bin/device-ap-mode ]; then
  echo "[spike-os] ABORT: set_up_completed is not true AND /usr/local/bin/device-ap-mode exists."
  echo "[spike-os] Enabling os-server at boot would drop this robot into AP mode and kill WiFi."
  echo "[spike-os] Finish setup in the web UI first, then re-run."
  exit 1
else
  echo "[spike-os] WARN: set_up_completed is not true, but /usr/local/bin/device-ap-mode does not"
  echo "[spike-os]       exist, so the AP switch is a no-op. Safe for now — finish setup before"
  echo "[spike-os]       that script ever lands on this robot."
fi
REMOTE_APCHECK

ssh "$REACHY_HOST" bash <<REMOTE_START
set -e
sudo tee /etc/systemd/system/$SERVICE.service >/dev/null <<'UNIT'
[Unit]
Description=Autonomous OS Server (spike layout)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
# config.Load reads the RELATIVE path config/config.json, so the working
# directory is what makes it /root/config/config.json — the same file HAL reads
# through OS_CONFIG_PATH. Running from anywhere else silently splits the two
# services onto different configs. The logger also writes /var/log/os-server.log,
# which needs root.
WorkingDirectory=/root
# DEVICE_TYPE explicitly, so a missing config still boots.
Environment=DEVICE_TYPE=reachy-mini
# DEVICES_DIR must be set too: device.DevicesDir() falls back to /opt/devices
# (devicemd.go), which the spike layout does not create — os-server then finds
# no DEVICE.md and reports an empty capability list, so the web Overview renders
# blank tiles while every service looks healthy.
Environment=DEVICES_DIR=$REMOTE_BASE/devices
ExecStart=$REMOTE_BASE/os-server
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=os-server

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable $SERVICE >/dev/null 2>&1
sudo systemctl restart $SERVICE

echo -n "[spike-os] waiting for os-server "
for i in \$(seq 1 30); do curl -sf localhost:5000/api/health/live >/dev/null 2>&1 && break; printf .; sleep 2; done; echo
echo "--- live ---";      curl -s localhost:5000/api/health/live; echo
echo "--- readiness ---"; curl -s localhost:5000/api/health/readiness; echo
REMOTE_START

cat <<EOF

========================================
  os-server running under systemd on $REACHY_HOST
    logs    : ssh $REACHY_HOST 'journalctl -u $SERVICE -f'
    API     : loopback only — from the Pi:  curl localhost:5000/api/health/live
    tunnel  : ssh -L 5000:localhost:5000 $REACHY_HOST
    restart : ssh $REACHY_HOST 'sudo systemctl restart $SERVICE'
    stop    : bash devices/reachy-mini/spike-os.sh --stop
========================================

It now comes back on its own after a reboot.

Footprint outside $REMOTE_BASE: /root/config/config.json and /var/log/os-server.log.
Web UI still needs nginx — see spike-os.sh header.
EOF
