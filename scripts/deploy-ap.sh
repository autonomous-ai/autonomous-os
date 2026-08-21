#!/usr/bin/env bash
# Deploy os-server binary to a device currently in AP (provisioning) mode.
#
# WHY THIS SCRIPT: when the device is in AP mode, its LAN IP is gone —
# your Mac is on the device's own hotspot (192.168.100.x), so the usual
# LAN-IP deploy path doesn't apply. This script scp's the pre-built binary
# to 192.168.100.1 (the AP static) and swaps it in.
#
# PRE-REQ:
#   1) Build the binary WHILE STILL ON INTERNET:
#        make os-build
#   2) Then join the device's AP hotspot on your Mac.
#   3) Run this script.
#
# USAGE:
#   scripts/deploy-ap.sh              # binary + restart os-server
#   scripts/deploy-ap.sh --web        # also rebuild + deploy web dist
#   scripts/deploy-ap.sh --host 192.168.100.1   # override target (default)
set -euo pipefail

HOST="192.168.100.1"
USER="orangepi"
PASS="orangepi"
SSHPASS_BIN="$HOME/.local/bin/sshpass"

DEPLOY_WEB=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --web)  DEPLOY_WEB=1; shift ;;
    --host) HOST="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$REPO_ROOT/system/os-server"
WEB_DIST="$REPO_ROOT/system/web/dist"

if [[ ! -x "$SSHPASS_BIN" ]]; then
  echo "ERROR: sshpass not found at $SSHPASS_BIN" >&2
  exit 1
fi
if [[ ! -f "$BIN" ]]; then
  echo "ERROR: binary not built at $BIN — run 'make os-build' first (needs internet)." >&2
  exit 1
fi
if [[ $DEPLOY_WEB -eq 1 && ! -d "$WEB_DIST" ]]; then
  echo "ERROR: web dist not built at $WEB_DIST — run 'make web-build' first." >&2
  exit 1
fi

# Preflight: verify we can actually reach the AP host. If Mac isn't on the
# hotspot yet, fail fast with an actionable message instead of a 30s SSH hang.
echo "=== Preflight: reach $HOST ==="
if ! ping -c 1 -W 2 "$HOST" >/dev/null 2>&1; then
  echo "ERROR: $HOST unreachable. Join the device's AP hotspot on your Mac first." >&2
  exit 1
fi
echo "OK"

SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=8)

echo
echo "=== 1) scp os-server binary → /tmp/os-server-new ==="
SSHPASS="$PASS" "$SSHPASS_BIN" -e scp "${SSH_OPTS[@]}" "$BIN" "$USER@$HOST:/tmp/os-server-new"

if [[ $DEPLOY_WEB -eq 1 ]]; then
  echo
  echo "=== 2) rsync web dist → /tmp/web-new/ ==="
  SSHPASS="$PASS" "$SSHPASS_BIN" -e rsync -az --delete \
    -e "ssh ${SSH_OPTS[*]}" "$WEB_DIST/" "$USER@$HOST:/tmp/web-new/"
fi

echo
echo "=== 3) sudo swap in place + restart os-server ==="
REMOTE_CMD='
  install -m 755 /tmp/os-server-new /usr/local/bin/os-server &&
'
if [[ $DEPLOY_WEB -eq 1 ]]; then
  REMOTE_CMD+='  rsync -a --delete /tmp/web-new/ /usr/share/nginx/html/setup/ &&
'
fi
REMOTE_CMD+='  systemctl restart os-server &&
  sleep 2 &&
  systemctl is-active os-server
'
# The AP portal's os-server restart briefly drops the HTTP socket the SSH
# session is orthogonal to, so this ssh stays connected across the restart.
SSHPASS="$PASS" "$SSHPASS_BIN" -e ssh "${SSH_OPTS[@]}" "$USER@$HOST" \
  "echo $PASS | sudo -S sh -c \"$REMOTE_CMD\""

echo
echo "✔ deploy complete."
echo "  Wi-Fi re-provision (new, isolated flow):  http://$HOST/wifi"
echo "  Full setup wizard (unchanged legacy):     http://$HOST/setup"
