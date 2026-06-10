#!/usr/bin/env bash
# Run this script directly on the Pi (as root) to add proxy_read_timeout /
# proxy_send_timeout to the existing /hw/ nginx location. Without it,
# long-running hardware endpoints (e.g. /hw/speaker/record-enroll) hit the
# default 60s nginx timeout and return 504.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root"
  exit 1
fi

DEVICE_TYPE="$(grep -E '^DEVICE_TYPE=' /opt/hal/.env 2>/dev/null | cut -d= -f2)"
CONF="/etc/nginx/conf.d/${DEVICE_TYPE}.conf"

if [ ! -f "$CONF" ]; then
  echo "Error: $CONF not found"
  exit 1
fi

if grep -q "proxy_read_timeout 300s" "$CONF"; then
  echo "[skip]  /hw/ already has 300s timeout"
else
  # Insert the two timeout directives right after the X-Forwarded-Prefix line
  # inside the /hw/ block. That line is unique to /hw/, so the anchor is safe.
  sed -i '/proxy_set_header X-Forwarded-Prefix \/hw;/a \    proxy_read_timeout 300s;\n    proxy_send_timeout 300s;' "$CONF"
  echo "[patch] Added proxy_read_timeout/proxy_send_timeout to /hw/"
fi

nginx -t
systemctl reload nginx
echo "[done]  nginx reloaded"
