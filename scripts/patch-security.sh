#!/usr/bin/env bash
# patch-security.sh — one-shot security patch for existing Lumi devices.
#
# Paste this entire script into the browser CLI (/monitor#cli) and press Enter.
# Safe to run multiple times.
#
# PREREQUISITE: run OTA first so the device has the latest code:
#   sudo software-update lelamp   ← same-origin middleware (server.py)
#   sudo software-update lumi     ← sameOriginOrLAN guard (/api/sensing/event)

set -euo pipefail

LELAMP_SVC="/etc/systemd/system/lumi-lelamp.service"
NGINX_CONF="/etc/nginx/conf.d/lumi.conf"

echo "[patch] Starting security patch..."

# 1. LeLamp systemd: bind 127.0.0.1 instead of 0.0.0.0
if grep -q "\-\-host 0.0.0.0" "$LELAMP_SVC" 2>/dev/null; then
  sed -i 's/--host 0\.0\.0\.0/--host 127.0.0.1/' "$LELAMP_SVC"
  systemctl daemon-reload
  echo "[patch] lumi-lelamp: bind changed to 127.0.0.1"
else
  echo "[patch] lumi-lelamp: already on 127.0.0.1, skipping"
fi

# 2. nginx /hw/: add allow/deny if missing
if ! grep -q "allow 127.0.0.1" "$NGINX_CONF" 2>/dev/null; then
  python3 - "$NGINX_CONF" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    content = f.read()

old = "  location /hw/ {\n    proxy_pass http://lelamp/;"
new = "  location /hw/ {\n    allow 127.0.0.1;\n    allow ::1;\n    deny all;\n    proxy_pass http://lelamp/;"
if old in content and new not in content:
    content = content.replace(old, new)
    with open(path, "w") as f:
        f.write(content)
    print("[patch] nginx /hw/: allow/deny added")
else:
    print("[patch] nginx /hw/: already patched, skipping")
PYEOF
else
  echo "[patch] nginx /hw/: already patched, skipping"
fi

# 3. nginx /api/system/exec: add allow/deny if missing
python3 - "$NGINX_CONF" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    content = f.read()

marker = "location = /api/system/exec {"
allow  = "allow 127.0.0.1;"

if marker in content and allow not in content:
    content = content.replace(
        "  location = /api/system/exec {\n    proxy_pass",
        "  location = /api/system/exec {\n    allow 127.0.0.1;\n    allow ::1;\n    deny all;\n\n    proxy_pass"
    )
    with open(path, "w") as f:
        f.write(content)
    print("[patch] nginx /api/system/exec: allow/deny added")
elif marker not in content:
    print("[patch] nginx /api/system/exec: block not found, may need manual check")
else:
    print("[patch] nginx /api/system/exec: already patched, skipping")
PYEOF

# 4. Set LELAMP_MODE=production in .env (activates same-origin middleware)
LELAMP_ENV="/opt/lelamp/.env"
touch "$LELAMP_ENV"
if grep -q "^LELAMP_MODE=" "$LELAMP_ENV" 2>/dev/null; then
  sed -i "s/^LELAMP_MODE=.*/LELAMP_MODE=production/" "$LELAMP_ENV"
  echo "[patch] LELAMP_MODE set to production"
else
  echo "LELAMP_MODE=production" >> "$LELAMP_ENV"
  echo "[patch] LELAMP_MODE=production added to .env"
fi

# 5. Add EnvironmentFile to lumi-lelamp.service if missing
if ! grep -q "^EnvironmentFile=" "$LELAMP_SVC" 2>/dev/null; then
  sed -i '/^\[Service\]/a EnvironmentFile=\/opt\/lelamp\/.env' "$LELAMP_SVC"
  systemctl daemon-reload
  echo "[patch] lumi-lelamp.service: EnvironmentFile added"
else
  echo "[patch] lumi-lelamp.service: EnvironmentFile already present, skipping"
fi

# 6. Apply
nginx -t && nginx -s reload
systemctl restart lumi-lelamp lumi

echo "[patch] Done. Device is patched."
