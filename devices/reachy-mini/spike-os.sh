#!/usr/bin/env bash
# spike-os.sh — install os-server on a Reachy Mini.
#
# RUNS ON THE ROBOT. Pulls the `os-server` component from OTA metadata, seeds a
# minimal config, and runs it under systemd.
#
# This used to cross-compile the Go binary on a Mac and scp it over. That was
# wrong twice: it required a Go toolchain and the repo on the developer's
# machine, and it shipped whatever happened to be in someone's working tree —
# not what the fleet runs, so a bug reproduced here said nothing about the
# build anyone else had. Everything now comes from the OTA feed, the same
# source the imager and scripts/provision/setup.sh read.
#
# Why root + WorkingDirectory=/root: config.Load reads the RELATIVE path
# config/config.json (system/server/config/config.go), so /root is what makes it
# /root/config/config.json — the same file HAL reads through OS_CONFIG_PATH.
# Running from anywhere else silently splits the two services onto different
# configs. The logger also writes /var/log/os-server.log, which needs root.
#
# The web UI is a separate step: os-server binds 127.0.0.1:5000 and serves no
# static files. Until spike-web.sh installs nginx, reach the API from the robot
# itself, or tunnel it:
#
#   ssh -L 5000:localhost:5000 pollen@reachy-mini.local
#
# Usage:
#   sudo bash spike-os.sh              # install + start
#   sudo bash spike-os.sh --stop
#   sudo bash spike-os.sh --uninstall  # stop + remove the unit and binary
set -euo pipefail

SPIKE_TAG="spike-os"
# shellcheck source=spike-lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/spike-lib.sh"

SERVICE="os-server"
STOP_ONLY=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --stop)      STOP_ONLY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    *) die "unknown flag: $arg" ;;
  esac
done

ensure_root "$@"

if [ "$STOP_ONLY" = "1" ] || [ "$UNINSTALL" = "1" ]; then
  say "Stopping os-server"
  stop_unit "$SERVICE"
  # Legacy path: earlier versions of this script ran os-server in tmux.
  tmux kill-session -t os 2>/dev/null || true
  pkill -f "$BIN_DIR/os-server" 2>/dev/null || true
  if [ "$UNINSTALL" = "1" ]; then
    remove_unit "$SERVICE"
    rm -f "$BIN_DIR/os-server"
    info "removed the unit and $BIN_DIR/os-server"
    info "kept $CONFIG_DIR/config.json — it holds provisioning state"
  else
    info "stopped; still enabled, so it returns on the next boot (--uninstall to prevent that)"
  fi
  exit 0
fi

say "1/4  Seed $CONFIG_DIR (config.json + bootstrap.json)"
# bootstrap.json before os-server starts, not at the bootstrap step: os-server
# reads OTAMetadataURL from that file and nowhere else, and the agent runtimes'
# skill watchers take the URL from it to fetch skills. Start os-server without
# it and skills stay empty — silently, since an unset URL is treated as "not
# provisioned yet" rather than an error. See ensure_bootstrap_config.
ensure_bootstrap_config
# Minimal config: the only fail-loud startup guard is device_type (server.go:
# "device_type unresolved … refusing to assume 'lamp'"). Everything else
# defaults and the web setup flow fills it in. Never overwrite an existing
# config — it holds provisioning state, keys and channels.
mkdir -p "$CONFIG_DIR"
if [ -f "$CONFIG_DIR/config.json" ]; then
  info "config.json already present — keeping it"
else
  cat >"$CONFIG_DIR/config.json" <<JSON
{
  "httpPort": 5000,
  "device_type": "$DEVICE_TYPE"
}
JSON
  info "seeded config.json for device_type=$DEVICE_TYPE"
fi

say "2/4  Check the AP-mode hazard before enabling at boot"
# os-server calls SwitchToAPMode() at startup whenever set_up_completed is false
# (system/server/config_watch.go). On a provisioned image that tears down the
# WiFi station — over SSH that means losing the robot until someone attaches a
# keyboard. Refuse rather than find out after the reboot.
if grep -q '"set_up_completed"[[:space:]]*:[[:space:]]*true' "$CONFIG_DIR/config.json" 2>/dev/null; then
  info "set_up_completed=true — no AP switch at boot"
elif [ -x /usr/local/bin/device-ap-mode ]; then
  die "set_up_completed is not true AND /usr/local/bin/device-ap-mode exists.
Enabling os-server at boot would drop this robot into AP mode and kill WiFi.
Finish setup in the web UI first, then re-run."
else
  info "WARN: set_up_completed is not true, but /usr/local/bin/device-ap-mode does not"
  info "WARN: exist, so the AP switch is a no-op. Safe for now — finish setup before"
  info "WARN: that script ever lands on this robot."
fi

say "3/4  Install the binary from OTA"
stop_unit "$SERVICE"
pkill -f "$BIN_DIR/os-server" 2>/dev/null || true
ota_install_binary os-server "$BIN_DIR/os-server"

say "4/4  Install the unit and start"
write_unit "$SERVICE" <<UNIT
[Unit]
Description=Autonomous OS Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
# config.Load reads config/config.json by a RELATIVE path — /root is what makes
# that $CONFIG_DIR/config.json, the same file HAL reads via OS_CONFIG_PATH.
WorkingDirectory=/root
# Explicit, so os-server still boots when config.json is missing or unreadable.
Environment=DEVICE_TYPE=$DEVICE_TYPE
Environment=DEVICES_DIR=$DEVICES_DIR
ExecStart=$BIN_DIR/os-server
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=os-server

[Install]
WantedBy=multi-user.target
UNIT

start_unit "$SERVICE"
wait_http "http://localhost:5000/api/health/live" 60 "os-server" || true
echo "--- live ---";      curl -s -m 5 localhost:5000/api/health/live; echo
echo "--- readiness ---"; curl -s -m 5 localhost:5000/api/health/readiness; echo

cat <<EOF

========================================
  os-server running under systemd
    version : $(ota_field os-server version)
    logs    : journalctl -u $SERVICE -f
    API     : loopback only — from the robot:  curl localhost:5000/api/health/live
    tunnel  : ssh -L 5000:localhost:5000 <user>@reachy-mini.local
    stop    : sudo bash spike-os.sh --stop
========================================

Footprint outside $BIN_DIR: $CONFIG_DIR/config.json and /var/log/os-server.log.
The web UI needs nginx — run spike-web.sh next.
EOF
