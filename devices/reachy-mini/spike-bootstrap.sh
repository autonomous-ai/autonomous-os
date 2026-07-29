#!/usr/bin/env bash
# spike-bootstrap.sh — install the OTA bootstrap worker on a Reachy Mini.
#
# RUNS ON THE ROBOT. Pulls the `bootstrap` component from OTA metadata, seeds
# /root/config/bootstrap.json, and runs it under systemd.
#
# What it is for: the other spike scripts install one version, once. The
# bootstrap worker is what keeps a robot current afterwards — it polls the OTA
# metadata and applies new os-server / hal / web / device builds without anyone
# SSHing in. On a spike robot that is the difference between "the port worked
# on the day we tried it" and a device that keeps up with the fleet.
#
# It is also what makes bootstrap.json authoritative: every other spike script
# reads metadata_url from that file (see spike-lib.sh), so a robot pointed at a
# staging feed keeps using it instead of silently falling back to production.
#
# Deliberately LAST in spike.sh. It can restart os-server and hal the moment it
# finds a newer build, and doing that while the rest of the stack is still being
# installed turns a clean bring-up into a race.
#
# Usage:
#   sudo bash spike-bootstrap.sh                       # install + start
#   sudo OTA_METADATA_URL=https://…/metadata.json \
#        bash spike-bootstrap.sh                       # point at another feed
#   sudo bash spike-bootstrap.sh --no-start            # install, leave it stopped
#   sudo bash spike-bootstrap.sh --stop
#   sudo bash spike-bootstrap.sh --uninstall
set -euo pipefail

SPIKE_TAG="spike-bootstrap"
# shellcheck source=spike-lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/spike-lib.sh"

SERVICE="bootstrap"
NO_START=0
STOP_ONLY=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --no-start)  NO_START=1 ;;
    --stop)      STOP_ONLY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    *) die "unknown flag: $arg" ;;
  esac
done

ensure_root "$@"

if [ "$STOP_ONLY" = "1" ] || [ "$UNINSTALL" = "1" ]; then
  say "Stopping the bootstrap worker"
  stop_unit "$SERVICE"
  if [ "$UNINSTALL" = "1" ]; then
    remove_unit "$SERVICE"
    rm -f "$BIN_DIR/bootstrap-server"
    info "removed the unit and $BIN_DIR/bootstrap-server"
    info "kept $CONFIG_DIR/bootstrap.json — the other spike scripts read metadata_url from it"
  else
    info "stopped; still enabled, so it returns on the next boot (--uninstall to prevent that)"
  fi
  exit 0
fi

say "1/3  Seed $CONFIG_DIR/bootstrap.json"
ensure_tools
mkdir -p "$CONFIG_DIR"
BS_JSON="$CONFIG_DIR/bootstrap.json"
URL="$(metadata_url)"

if [ -f "$BS_JSON" ]; then
  # Merge-if-empty, never clobber: an operator who pointed this robot at a
  # staging feed must not be silently moved back to production by a re-run.
  TMP="$(mktemp)"
  if jq --arg url "$URL" \
      'if (.metadata_url // "") == "" then .metadata_url = $url else . end' \
      "$BS_JSON" >"$TMP" 2>/dev/null; then
    mv "$TMP" "$BS_JSON"
    info "kept the existing config; metadata_url=$(jq -r '.metadata_url' "$BS_JSON")"
  else
    rm -f "$TMP"
    info "WARN: $BS_JSON is not valid JSON — leaving it untouched"
  fi
else
  jq -n --arg url "$URL" \
    '{httpPort: 8080, metadata_url: $url, poll_interval: "5m", state_file: "/root/bootstrap/state.json"}' \
    >"$BS_JSON"
  info "seeded $BS_JSON with metadata_url=$URL"
fi
mkdir -p /root/bootstrap

say "2/3  Install the binary from OTA"
# Stop first: replacing a running binary in place gives ETXTBSY.
stop_unit "$SERVICE"
ota_install_binary bootstrap "$BIN_DIR/bootstrap-server"

say "3/3  Install the unit"
write_unit "$SERVICE" <<UNIT
[Unit]
Description=Autonomous OTA Bootstrap Worker
# Needs a route to the CDN before its first poll; without this it burns its
# first interval failing DNS on a cold boot.
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
# Reads config/bootstrap.json by a RELATIVE path, the same convention os-server
# uses for config/config.json — the working directory is what makes it
# $CONFIG_DIR/bootstrap.json.
WorkingDirectory=/root
ExecStart=$BIN_DIR/bootstrap-server
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=bootstrap

[Install]
WantedBy=multi-user.target
UNIT

if [ "$NO_START" = "1" ]; then
  systemctl enable "$SERVICE" >/dev/null 2>&1
  info "--no-start: installed and enabled, not started"
else
  start_unit "$SERVICE"
  sleep 2
  systemctl is-active --quiet "$SERVICE" \
    && info "running" \
    || info "WARN: not running — journalctl -u $SERVICE -n 50"
fi

cat <<EOF

========================================
  OTA bootstrap worker installed
    feed    : $URL
    poll    : $(jq -r '.poll_interval // "5m"' "$BS_JSON")
    logs    : journalctl -u $SERVICE -f
    stop    : sudo bash spike-bootstrap.sh --stop
========================================

From here the robot updates itself: new os-server / hal / web / device builds
are picked up on the next poll. Publish with the make upload-* targets.
EOF
