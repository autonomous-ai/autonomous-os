# Security Test Checklist

Branch: `feat/security` — PR #69

Pi IP (Tailscale): `100.119.50.21`  
Run these from any machine on the team that has Tailscale access.

---

## Results legend

| Symbol | Meaning |
|--------|---------|
| ✅ PASS | Response matches expected — secure |
| ❌ FAIL | Response does NOT match — something is wrong |

---

## F1 — LeLamp direct port blocked (not exposed to LAN)

LeLamp binds to `127.0.0.1:5001`, never reachable directly from outside.

```bash
curl -v --connect-timeout 5 http://100.119.50.21:5001/hw/health
```

**Expected:** `curl: (7) Failed to connect` or connection timeout  
**FAIL if:** returns any HTTP response

---

## F2 — nginx `/hw/` accessible in developer mode

`LELAMP_MODE=developer` → middleware disabled, nginx does NOT block `/hw/`.

```bash
curl -s -o /dev/null -w "%{http_code}" http://100.119.50.21/hw/health
```

**Expected:** `200`  
**FAIL if:** returns `403` or connection refused

---

## F3 — LeLamp local-only middleware (production mode simulation)

In production mode, any non-localhost caller gets 403 from Python middleware.  
To test without changing the Pi, hit `/hw/` and check response header — in developer mode it passes through.

```bash
curl -s http://100.119.50.21/hw/health | python3 -c "import sys,json; d=json.load(sys.stdin); print('status:', d.get('status'))"
```

**Expected:** `status: 1` (request passes through in developer mode)  
**Note:** Switch `LELAMP_MODE=production` in Pi `.env` + restart to test the block path

---

## F5a — `POST /api/system/exec` blocked (RCE endpoint)

This endpoint must NEVER be reachable from outside localhost.

```bash
curl -s -o /dev/null -w "%{http_code}" -X POST http://100.119.50.21/api/system/exec \
  -H "Content-Type: application/json" \
  -d '{"cmd":"ls"}'
```

**Expected:** `403`  
**FAIL if:** returns `200` or any other non-403 (means RCE is open 💀)

---

## F5b — `WS /api/system/shell` reachable (intentional — Web UI terminal)

Shell WebSocket must still be reachable from LAN for the xterm.js terminal in the Web UI.

```bash
curl -s -o /dev/null -w "%{http_code}" --max-time 3 \
  -H "Upgrade: websocket" \
  -H "Connection: Upgrade" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  -H "Sec-WebSocket-Version: 13" \
  http://100.119.50.21/api/system/shell
```

**Expected:** `101` (WebSocket upgrade accepted)  
**FAIL if:** returns `403` (would break the Web UI terminal)

---

## F6 — `/gw/` OpenClaw UI loads but WebSocket requires HTTPS

UI HTML loads, but WebSocket connection requires a secure context (HTTPS or localhost).

```bash
curl -s -o /dev/null -w "%{http_code}" http://100.119.50.21/gw/
```

**Expected:** `200` (HTML loads)  
**Note:** Connecting via browser over HTTP will show "requires HTTPS or localhost secure context" — this is expected browser security behavior, not a bug.

---

## F7 — DL Backend requires API key (RunPod only)

Replace `<POD_ID>` with the actual RunPod pod ID.

```bash
# Without key — must be rejected
curl -s -o /dev/null -w "%{http_code}" \
  https://<POD_ID>-8888.proxy.runpod.net/api/dl/health

# With correct key — must work
curl -s -o /dev/null -w "%{http_code}" \
  -H "X-API-Key: <your-DL_API_KEY>" \
  https://<POD_ID>-8888.proxy.runpod.net/api/dl/health
```

**Expected (no key):** `401`  
**Expected (with key):** `200`  
**FAIL if:** no-key request returns `200` (means DL_API_KEY was not set at startup)

---

## F8b — DL Backend encryption (when enabled)

When `CRYPTO__ENABLED=true` on the LB and `CRYPTO__REQUIRE_ENCRYPTION=true`:

```bash
# Public key endpoint must return PEM
curl -s -H "X-API-Key: <your-DL_API_KEY>" \
  https://<POD_ID>-8888.proxy.runpod.net/api/crypto/public-key \
  | head -1
```

**Expected:** `-----BEGIN PUBLIC KEY-----`
**FAIL if:** returns 404 or empty (crypto not enabled on LB)

```bash
# Plaintext request must be rejected when require_encryption=true
curl -s -o /dev/null -w "%{http_code}" \
  -H "X-API-Key: <your-DL_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"image_b64":"test","threshold":0.5}' \
  https://<POD_ID>-8888.proxy.runpod.net/api/dl/emotion-recognize
```

**Expected:** `400` (encryption required)
**FAIL if:** returns `200` (plaintext accepted when require_encryption=true)

---

## Findings not patched in this PR — rationale

These were reviewed and explicitly deferred. They are not oversights.

### F4 — Lamp Go wildcard CORS (`Access-Control-Allow-Origin: *`)

Not patched because CORS only matters when a browser can reach the endpoint.
Every high-risk endpoint is already blocked at the nginx layer before a browser
request arrives (`/api/system/exec` → 403, `/hw/*` → 403).
For the remaining public UI endpoints (`/api/health`, setup flow, etc.) wildcard
CORS is acceptable: they carry no secrets and are intentionally browser-accessible.

Revisit if a sensitive non-admin endpoint is added to `/api/` in the future.

### F6 — `/gw/` OpenClaw gateway proxied by nginx

Not patched because the browser enforces a secure-context requirement on its own:
WebSocket connections from an HTTP page to a non-localhost host are blocked by the
browser (`mixed content` / `secure context` policy).
Connecting to the Control UI over `http://` produces:
> "control ui requires device identity (use HTTPS or localhost secure context)"

Practical impact: a LAN attacker who loads the page gets an HTML shell but cannot
establish the WebSocket session needed to read conversations or send commands.
OpenClaw also requires device-identity + OAuth before any agent action.

Revisit if HTTPS is added to the Pi (at that point the WebSocket gate opens and
the nginx `allow/deny` block from the audit becomes necessary).

### F8 — OpenClaw `controlUi.allowedOrigins: ["*"]`

Depends on F6. The wildcard origins only become dangerous if the `/gw/` WebSocket
is reachable from a browser, which requires HTTPS (see F6 above). Until HTTPS is
added, this setting has no practical effect on the attack surface.
Will be tightened together with F6 when HTTPS is introduced.

### F9 — Docs describe external Swagger as intentional

The relevant doc sections were written before the local-only boundary was designed.
They are factually wrong but do not affect runtime security.
Low-priority cleanup; will be corrected as part of the next doc refresh cycle.

---

## Quick all-in-one script

Copy-paste to run all Pi tests at once:

```bash
PI=100.119.50.21

echo "=== F1: LeLamp direct port ==="
curl -v --connect-timeout 5 http://$PI:5001/hw/health 2>&1 | grep -E "connect|HTTP" | head -3

echo ""
echo "=== F2: /hw/ accessible (developer mode) ==="
echo "HTTP $(curl -s -o /dev/null -w '%{http_code}' http://$PI/hw/health) — expect 200"

echo ""
echo "=== F5a: /api/system/exec blocked ==="
echo "HTTP $(curl -s -o /dev/null -w '%{http_code}' -X POST http://$PI/api/system/exec -H 'Content-Type: application/json' -d '{"cmd":"ls"}') — expect 403"

echo ""
echo "=== F5b: /api/system/shell WebSocket reachable ==="
echo "HTTP $(curl -s -o /dev/null -w '%{http_code}' --max-time 3 -H 'Upgrade: websocket' -H 'Connection: Upgrade' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' -H 'Sec-WebSocket-Version: 13' http://$PI/api/system/shell) — expect 101"

echo ""
echo "=== F6: /gw/ loads ==="
echo "HTTP $(curl -s -o /dev/null -w '%{http_code}' http://$PI/gw/) — expect 200"
```
