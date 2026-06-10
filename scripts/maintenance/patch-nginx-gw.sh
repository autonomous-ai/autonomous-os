#!/usr/bin/env bash
# Run this script directly on the Pi (as root) to proxy OpenClaw gateway at /gw/
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

if ! grep -q "upstream openclaw" "$CONF"; then
  sed -i '/^upstream hal/a upstream openclaw { server 127.0.0.1:18789; }' "$CONF"
  echo "[patch] Added upstream openclaw"
else
  echo "[skip]  upstream openclaw already present"
fi

if ! grep -q "location /gw/" "$CONF"; then
  sed -i '/# Return 204 so OS/i \
  location /gw/ {\
    proxy_pass http://openclaw/;\
    proxy_http_version 1.1;\
    proxy_set_header Upgrade $http_upgrade;\
    proxy_set_header Connection "upgrade";\
    proxy_set_header Host $host;\
    proxy_set_header X-Real-IP $remote_addr;\
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\
  }\
' "$CONF"
  echo "[patch] Added location /gw/"
else
  echo "[skip]  location /gw/ already present"
fi

nginx -t
systemctl reload nginx
echo "[done]  nginx reloaded"
echo "  HTTP:  http://$(hostname -I | awk '{print $1}')/gw/"
echo "  WS:    ws://$(hostname -I | awk '{print $1}')/gw/"
