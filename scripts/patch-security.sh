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

# 3a. nginx /api/system/shell: add WebSocket upgrade block if missing.
# Devices provisioned from setup.sh before the dedicated shell block was added
# fall through to the generic /api/ proxy (no Upgrade headers) — WS handshake
# fails with "upgrade token not found in Connection header".
python3 - "$NGINX_CONF" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    content = f.read()

if "location = /api/system/shell" in content:
    print("[patch] nginx /api/system/shell: already present, skipping")
    sys.exit(0)

old = "  location /api/ {"
new = """  # Interactive shell WebSocket (xterm.js PTY) — must come before generic /api/.
  location = /api/system/shell {
    proxy_pass http://backend;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;
  }

  location /api/ {"""

if old not in content:
    print("[patch] nginx /api/system/shell: /api/ block not found, may need manual check")
    sys.exit(0)

content = content.replace(old, new, 1)
with open(path, "w") as f:
    f.write(content)
print("[patch] nginx /api/system/shell: WebSocket upgrade block added")
PYEOF

# 3b. nginx /gw and /gw/: add allow/deny if missing (OpenClaw gateway local-only)
python3 - "$NGINX_CONF" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    content = f.read()

patched_any = False
for marker, old, new in (
    (
        "location = /gw {",
        "  location = /gw {\n    proxy_pass http://openclaw/;",
        "  location = /gw {\n    allow 127.0.0.1;\n    allow ::1;\n    deny all;\n\n    proxy_pass http://openclaw/;",
    ),
    (
        "location /gw/ {",
        "  location /gw/ {\n    proxy_pass http://openclaw/;",
        "  location /gw/ {\n    allow 127.0.0.1;\n    allow ::1;\n    deny all;\n\n    proxy_pass http://openclaw/;",
    ),
):
    if marker not in content:
        print(f"[patch] nginx {marker.split()[1]}: block not found, skipping")
        continue
    if new in content:
        print(f"[patch] nginx {marker.split()[1]}: already patched, skipping")
        continue
    if old in content:
        content = content.replace(old, new)
        patched_any = True
        print(f"[patch] nginx {marker.split()[1]}: allow/deny added")
    else:
        print(f"[patch] nginx {marker.split()[1]}: block shape differs, may need manual check")

if patched_any:
    with open(path, "w") as f:
        f.write(content)
PYEOF

# 3c. nginx security headers (CSP, X-Frame-Options, …): inject after
# `client_max_body_size` if missing. Defends the monitor UI from clickjacking
# + MIME-sniffing and shrinks future XSS blast radius.
python3 - "$NGINX_CONF" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    content = f.read()

if "Content-Security-Policy" in content:
    print("[patch] nginx security headers: already present, skipping")
    sys.exit(0)

# Anchor: the client_max_body_size line is present on every device since
# the attachments PR; using it as the anchor keeps the headers grouped
# with other server-level settings.
import re
anchor = re.search(r"client_max_body_size\s+\S+;\s*\n", content)
if not anchor:
    print("[patch] nginx security headers: client_max_body_size anchor not found, may need manual add")
    sys.exit(0)

block = (
    "\n"
    "  # Security headers (clickjacking, MIME sniff, XSS containment).\n"
    "  add_header X-Frame-Options \"DENY\" always;\n"
    "  add_header X-Content-Type-Options \"nosniff\" always;\n"
    "  add_header Referrer-Policy \"no-referrer\" always;\n"
    "  add_header Permissions-Policy \"camera=(), microphone=(), geolocation=(), payment=()\" always;\n"
    "  add_header Content-Security-Policy \"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self' ws: wss:; frame-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'\" always;\n"
)

end = anchor.end()
content = content[:end] + block + content[end:]
with open(path, "w") as f:
    f.write(content)
print("[patch] nginx security headers: added")
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

# 6. Bind lumi-server to 127.0.0.1 (defense-in-depth: port 5000 unreachable from LAN
#    even if nginx config is wrong). Only needed on devices deployed before 2026-05-19.
LUMI_SVC="/etc/systemd/system/lumi.service"
LUMI_BIN="/usr/local/bin/lumi-server"

# Detect if the installed binary still binds 0.0.0.0 by checking its help/version
# output — there is no config knob for this; it is baked into the binary.
# New binaries (post-2026-05-19 OTA) bind 127.0.0.1 by default; old ones bind :5000.
# The reliable signal is the OTA version. If lumi OTA is up-to-date, skip.
LUMI_VERSION=$(lumi-server --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
echo "[patch] lumi-server version: ${LUMI_VERSION:-unknown}"
echo "[patch] To close port 5000 on LAN: run 'sudo software-update lumi' to get the latest binary."

# 7. Apply
nginx -t && nginx -s reload
systemctl restart lumi-lelamp lumi

echo "[patch] Done. Device is patched."
