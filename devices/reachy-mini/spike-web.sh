#!/usr/bin/env bash
# spike-web.sh — Serve the web UI on a Reachy Mini via nginx.
#
# Runs FROM YOUR MAC. Third and last spike script: spike-hal.sh brings up the
# body, spike-os.sh brings up the API, this one puts a browser in front of them.
# Builds the Vite bundle, installs nginx, writes a spike vhost, and reloads.
#
# Why nginx at all: os-server binds 127.0.0.1:5000 and serves no static files
# (no StaticFS/embed.FS anywhere in system/). nginx is what serves the dist and
# proxies /api to os-server — without it the bundle is dead weight on disk.
#
# This is NOT the production nginx config. scripts/provision/setup.sh writes the
# real one (security headers, CSP, captive portal, /gw OpenClaw upgrade, admin
# shell routes). This vhost is the minimum that makes the UI usable during a
# spike, so the two must not be confused: the production file lands in
# /etc/nginx/conf.d/<device_type>.conf, this one in sites-available/reachy-spike.
#
# Usage:
#   bash devices/reachy-mini/spike-web.sh              # build + install + serve
#   bash devices/reachy-mini/spike-web.sh --no-build   # reuse system/web/dist
#   bash devices/reachy-mini/spike-web.sh --stop       # disable the vhost (nginx stays installed)
set -euo pipefail

REACHY_HOST="${REACHY_HOST:-pollen@reachy-mini.local}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REMOTE_BASE="/opt/autonomous"
WEB_DIR="$REMOTE_BASE/web"
VHOST="reachy-spike"

SKIP_BUILD=0
STOP_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --no-build) SKIP_BUILD=1 ;;
    --stop)     STOP_ONLY=1 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n========== %s ==========\n' "$1"; }

if [ "$STOP_ONLY" = "1" ]; then
  say "Disabling the spike vhost"
  ssh "$REACHY_HOST" "sudo rm -f /etc/nginx/sites-enabled/$VHOST && sudo systemctl reload nginx 2>/dev/null || true; echo 'vhost disabled (nginx still installed)'"
  exit 0
fi

echo "target: $REACHY_HOST"

if [ "$SKIP_BUILD" = "0" ]; then
  say "1/4  Build web UI"
  cd "$ROOT_DIR"
  make web-build
else
  say "1/4  Build skipped (--no-build)"
fi
[ -d "$ROOT_DIR/system/web/dist" ] || { echo "no bundle at system/web/dist — run without --no-build"; exit 1; }

say "2/4  Copy bundle to $WEB_DIR"
ssh "$REACHY_HOST" "sudo mkdir -p $WEB_DIR && sudo chown -R \$(id -u):\$(id -g) $WEB_DIR"
rsync -az --delete "$ROOT_DIR/system/web/dist/" "$REACHY_HOST:$WEB_DIR/"
# nginx runs as www-data and must traverse every parent directory.
ssh "$REACHY_HOST" "sudo chmod 755 /opt $REMOTE_BASE $WEB_DIR"

say "3/4  Install nginx + spike vhost"
ssh "$REACHY_HOST" bash <<REMOTE_NGINX
set -e
command -v nginx >/dev/null || { echo "[spike-web] installing nginx..."; sudo apt-get update -qq && sudo apt-get install -y nginx; }

# Debian's default vhost also claims :80 default_server — two default_servers is
# a config error, so drop it. Kept out of --stop's undo on purpose: restoring a
# default page nobody wants is not cleanup.
sudo rm -f /etc/nginx/sites-enabled/default

sudo tee /etc/nginx/sites-available/$VHOST >/dev/null <<'NGINX'
upstream spike_backend { server 127.0.0.1:5000; }
upstream spike_hal     { server 127.0.0.1:5001; }

server {
  listen 80 default_server;
  root $WEB_DIR;
  index index.html;

  # Monitor chat attaches base64 payloads; nginx's 1 MB default 413s them.
  client_max_body_size 20M;

  # Same security posture as the production vhost (scripts/imager/build-orangepi.sh)
  # — this is a device-control UI on a shared network, not a demo page.
  add_header X-Frame-Options "SAMEORIGIN" always;
  add_header X-Content-Type-Options "nosniff" always;
  add_header Referrer-Policy "no-referrer" always;
  add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
  add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; media-src 'self' blob:; connect-src 'self' ws: wss: http:; frame-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'; form-action 'self'" always;

  # Cache policy uses `expires`, NOT `add_header Cache-Control`: an add_header
  # inside a location cancels inheritance of every server-level add_header,
  # which would silently drop the CSP and security headers above.
  # index.html must never be cached — it is the pointer to fingerprinted asset
  # names, and a stale copy asks for bundles the last deploy deleted (blank page).
  location = /index.html { expires -1; }
  location /assets/      { expires 1y; }

  # SPA fallback — client-side routes are not files on disk.
  location / {
    try_files \$uri /index.html;
  }

  location /api/ {
    proxy_pass http://spike_backend;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    # No proxy_buffering off here: os-server sets X-Accel-Buffering: no on its
    # own SSE responses (flow, monitor, logs), so nginx unbuffers exactly those.
  }

  # WebSocket endpoints need an explicit upgrade + a long read timeout.
  location = /api/system/shell {
    proxy_pass http://spike_backend;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
    proxy_read_timeout 86400s;
  }

  location = /api/buddy/ws {
    proxy_pass http://spike_backend;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
    proxy_read_timeout 86400s;
  }

  # HAL is loopback-only, exactly as in production. The browser must reach
  # hardware through os-server's authenticated /api/hardware/* proxy — exposing
  # /hw/ to the LAN would hand every host on the network unauthenticated servo,
  # camera, and audio control.
  location /hw/ {
    allow 127.0.0.1;
    allow ::1;
    deny all;
    proxy_pass http://spike_hal/;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Prefix /hw;
    # Hardware calls can run long (enroll records for seconds, then embeds).
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
  }
}
NGINX

sudo ln -sfn /etc/nginx/sites-available/$VHOST /etc/nginx/sites-enabled/$VHOST
sudo nginx -t
sudo systemctl reload nginx || sudo systemctl restart nginx
echo "[spike-web] nginx reloaded"
REMOTE_NGINX

say "4/4  Verify from the Pi"
ssh "$REACHY_HOST" bash <<'REMOTE_CHECK'
set -e
code() { curl -s -o /dev/null -w "%{http_code}" -m 10 "$1"; }
echo "[spike-web] GET /              -> $(code http://localhost/)"
echo "[spike-web] GET /api/health/live -> $(code http://localhost/api/health/live)"
echo "[spike-web] GET /hw/health       -> $(code http://localhost/hw/health)"
REMOTE_CHECK

cat <<EOF

========================================
  Web UI served on $REACHY_HOST
    browser : http://${REACHY_HOST#*@}/
    vhost   : /etc/nginx/sites-available/$VHOST  (spike-only, NOT the production config)
    bundle  : $WEB_DIR
    stop    : bash devices/reachy-mini/spike-web.sh --stop
========================================

Footprint: the nginx package, /etc/nginx/sites-{available,enabled}/$VHOST, and
the removed Debian default vhost. Uninstall: --stop, then
  sudo apt remove --purge nginx && sudo rm -f /etc/nginx/sites-available/$VHOST
EOF
