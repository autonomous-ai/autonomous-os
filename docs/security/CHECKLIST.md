# Security Audit Checklist

Consolidated status of all 3 audits in `docs/security/`. Last verified: 2026-05-20.

Status legend: ✅ done · ⚠️ partial / decision needed · ❌ not done · ➖ skipped intentionally

Work credit: PRs by `31803smith` — #69 (aa98a207), #77 (e9d8a1f1), #79 (039b25b9), #81 (f4c0cd3a).

---

## 1. [`local-only-boundary.md`](./local-only-boundary.md) — Local-only API boundary

| # | Finding | Status | Notes |
|---|---|---|---|
| F1 | HAL bind 0.0.0.0 → 127.0.0.1 | ✅ | PR #69 — `HAL_MODE=production` default + `--host 127.0.0.1` in setup.sh, build.sh, server.py |
| F2 | nginx `/hw/` deny LAN | ✅ | PR #69 — `allow 127.0.0.1; deny all;` in setup.sh + build.sh |
| F3 | HAL local-only middleware | ✅ | PR #69 `local_only_middleware`, evolved to **same-origin** in PR #77, **+ bearer token** path added 2026-05-19. Three allow paths: loopback / `Authorization: Bearer <llm_api_key>` / same-origin. The Go client auto-injects the bearer (`os/services/lib/hal/client.go`) |
| F4 | Lamp wildcard CORS | ✅ | PR #79 (`b7d5bc49`) — drop `*`, allow same-host + `lamp-*.local` + `*.autonomous.ai` via shared `isAllowedOrigin` |
| F5a | `/api/system/exec` lockdown | ✅ | PR #69 nginx allow/deny + PR #81 Go `localOnlyMiddleware` (defense in depth) |
| F5b | `/api/system/shell` lockdown | ✅ | 2026-05-20 Login UI batch: shell now sits behind `adminAuthMiddleware` (cookie or Bearer). LAN access without auth is no longer possible — the operator must sign in first |
| F5c | `/api/openclaw/config-json` lockdown | ✅ | PR #81 — `localOnlyMiddleware` (stricter than the audit recommended) |
| F6 | nginx `/gw/` deny LAN | ✅ | 2026-05-19: `allow 127.0.0.1; allow ::1; deny all;` on `location = /gw` + `location /gw/` in `scripts/provision/setup.sh` + `imager/build.sh` + `scripts/maintenance/patch-security.sh` (section 3b for existing devices) |
| F7a | DL backend `DL_API_KEY` mandatory | ✅ | PR #69 — `field_validator` raises when empty. Still applies as a code-level check, deployment-agnostic |
| F7b | DL backend bind default 127.0.0.1 | ➖ | **Out of scope for this device.** dlbackend deploys on a separate server (GPU box); Lamp/HAL reach it through a proxy/LB using `llm_api_key`. The bind default in `dlbackend/Makefile` only affects local dev runs and is unrelated to the device threat model |
| F8 | OpenClaw `controlUi` tighten | ✅ | 2026-05-19: `setup.sh:586-589` sets `["http://127.0.0.1", "http://localhost"]` + `allowInsecureAuth=false`. `os/services/internal/openclaw/onboarding.go::ensureControlUIConfig()` tightens defaults and migrates existing devices with loose defaults (`["*"]` + `true`) to strict on every boot |
| F9 | Docs `/hw/*` external | ✅ | PR #69 update devices/lamp/docs/architecture-decision.md + bootstrap-ota.md (+vi). Bonus: `253a1e44` made /hw/docs iframe-only |

---

## 2. [`web-frontend-audit.md`](./web-frontend-audit.md) — Web frontend

| # | Finding | Status | Notes |
|---|---|---|---|
| F1 | Frontend fetch raw `/api/device/config` secrets | ✅ | 2026-05-20: Backend `ConfigPublicResponse` returns `has_*` booleans only. Frontend `EditConfig` / `useConfigPrefill` switched to read booleans; secret fields stay empty until the operator types via `SecretUpdateField` |
| F2 | Edit-mode `LockedField` reveals tokens | ✅ | 2026-05-20: New `SecretUpdateField` (write-only). `edit/ChannelSection.tsx` uses it for telegram/slack/discord bot tokens. Operators can no longer view saved tokens — only overwrite |
| F3 | Setup accepts secrets in URL query params | ✅ | 2026-05-20: `App.tsx` calls `scrubLocationSecrets()` on every mount → `window.history.replaceState` drops `tele_token`, `llm_api_key`, `password`, `admin_password`, etc. from the URL bar / history without reloading. The Setup form still reads them once via `useSetupUrlParams` before the scrub |
| F4 | Frontend fetch `/api/openclaw/config-json` → `#token=` | ✅ | 2026-05-20: `monitor/index.tsx` `AgentGWMenu` dropped the fetch + `#token=` fragment build. `GwConfig.tsx` shows a "no longer exposed via HTTP" message. `ChatSection.tsx` pulls the model label from `/api/device/config` instead |
| F5 | Frontend direct call `/hw/*` | ✅ | 2026-05-19: Go wildcard reverse proxy `/api/hardware/*` (`adminAuthMiddleware` + bearer or `?token=`). Web `HW` constant renamed `/hw` → `/api/hardware`, the `fetch` interceptor auto-attaches Bearer, and the `hwUrl()` helper covers `<img>` / `<a>` / `window.open`. Nginx `/hw/` `allow 127.0.0.1; deny all;` stays as-is (audit F2 intact) |
| F6 | Web UI shell (`CliSection`) | ✅ | 2026-05-20 **decision locked**: 3-layer defense is enough — (1) backend `GET /api/system/shell` admin-auth gated (Login UI batch closed F4); WebSocket upgrade fails 401 without cookie/Bearer. (2) Sidebar nav hides CliSection unless `?debug=true`. (3) Even if a caller hits `#cli` directly, the WS attempt is rejected by admin auth. Production bundle still ships ~20KB of xterm.js code (purely cosmetic — no exploitable surface). Code-strip via `import.meta.env.DEV` rejected: pure UX / bundle-size concern, no security delta |
| F7 | Chat history persists in `localStorage` | ✅ | 2026-05-20: `HISTORY_TTL_MS = 7 days` envelope `{savedAt, convos}` → auto-purge stale. Clear button (Trash2) in the chat header with a confirm dialog. Backward compatible with the legacy array shape |
| F8 | `noopener noreferrer` + URL encoding | ✅ | 2026-05-20: Audited `target="_blank"` (3 sites) + `window.open` (2 sites). Fixed `rel="noreferrer"` → `noopener noreferrer` in `monitor/index.tsx` Gateway link + `edit/VoiceSection.tsx`. `window.open` calls now pass features `"noopener,noreferrer"`. Photo/audio path segments use `encodeURIComponent()` |
| F9 | No CSP / X-Frame-Options headers | ✅ | 2026-05-20: headers in `setup.sh` + `imager/build.sh` + `patch-security.sh` — `X-Frame-Options: SAMEORIGIN`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy` disables camera/mic/geo/payment, **strict CSP** (`default-src 'self'`, `script-src 'self'`, `style-src 'self' 'unsafe-inline'` for React style props, `img-src 'self' data: blob:`, `font-src 'self' data:`, `connect-src 'self' ws: wss: http://*.local`, `frame-ancestors 'self'`). The initial loosening for the in-iframe Swagger UI was reverted on the same day by self-hosting Swagger UI assets in HAL (`os/hal/static/swagger-ui-bundle.js` + `swagger-ui.css` + external `swagger-init.js`) and serving a no-inline-script HTML from a custom `/docs` handler. FastAPI `servers=[…]` lets Swagger UI "Try it out" call through the `/api/hardware/*` proxy in the browser flow. `connect-src` includes `http://*.local` because the Setup page probes `http://<host>.local/api/health` to auto-redirect after the AP→home-WiFi handoff (`useSetupStatusPolling.ts`); without it the probe is CSP-blocked and the "joining Wi-Fi…" screen never advances |
| F10 | No central authenticated fetch client | ➖ | 2026-05-20 **decision skip**: `lib/api.ts` patches `window.fetch` globally — sets `credentials: "include"` + auto-attaches `Authorization: Bearer` for every `/api/*` request. Browser cookie auth therefore rides every existing raw `fetch()` site without a refactor. The original audit suggestion (extract a typed `apiFetch()` wrapper) is purely cosmetic now — would touch ~65 sites for no security delta. Skip until a future refactor lands organically |
| F11 | Setup redirect preserves secret query params | ✅ | 2026-05-20: `safeSearch()` helper in `lib/api.ts` (10 secret query keys), applied to `App.tsx` lan_ip redirect + `useSetupStatusPolling.ts` (×2 redirects) + `Setup.tsx` mDNS link |
| F12 | `/hw/docs` iframe in monitor | ✅ | 2026-05-20: Iframe rewired to `/api/hardware/docs` (Go reverse proxy, admin-auth gated). New top-level route `GET /openapi.json` (Go) + nginx location proxies to HAL via the same auth gate so Swagger UI can fetch its spec. Outsiders without cookie/Bearer → 401 on both paths. Iframe is `debug=true`-gated in `monitor/index.tsx` so production operators don't see it unless they opt in. The audit's original "remove entirely" recommendation was traded for "gate + render through authed proxy"; see F9 for the CSP trade-off this required |
| F13 | TTS preview ships API key via `/hw/voice/speak` | ✅ | 2026-05-20: New Go endpoint `POST /api/voice/preview` (`adminAuthMiddleware`) reads `cfg.GetTTSAPIKey()` + `cfg.GetTTSBaseURL()` server-side and forwards to HAL `/voice/speak`. Web `testTTSVoice` ships `{text, voice, provider}` only — no secrets in the body. `lelamp.SpeakPreview()` helper handles partial overrides |
| F14 | Raw `/hw/face/photo/*` URLs in the DOM | ✅ | 2026-05-20: Login UI batch — `hwUrl()` no longer appends `?token=` when cookie auth is in play; the browser session cookie auto-attaches to same-origin `<img>` / `<a>` / `window.open` / MJPEG. Legacy Bearer fallback still rides `?token=` for scripted callers. Opaque IDs deferred (not part of cookie-auth scope) |

---

## 3. [`go-server-audit.md`](./go-server-audit.md) — Go server

| # | Finding | Status | Notes |
|---|---|---|---|
| F1 | No auth on `/api/*` | ✅ | 2026-05-19 `adminAuthMiddleware` (Bearer = `llm_api_key`). 2026-05-20 Login UI batch: the middleware also accepts a `lamp_session` HMAC cookie set by `POST /api/login` (bcrypt verifies `admin_password_hash`). `GET /api/device/config` is gated (returns `ConfigPublicResponse` — `has_*` booleans, no secrets). 2026-05-20 follow-up: every `/api/openclaw/*` route (status, events, flow-stream, flow-events, recent, flow-logs, analytics, compaction-latest, mood/wellbeing/posture/music-suggestion histories, tts/stop, busy) is now admin-gated — conversation history + behavioural data require auth. `config-json` keeps `localOnlyMiddleware` (stricter than admin auth). Remaining open endpoints are intentional pre-auth bootstrap (`/api/health/*`, `/api/network/*`, `/api/device/setup/status`, `/api/device/voices`, `/api/device/tts-providers`, `/api/system/{info,network,dashboard}`) and `sameOriginOrLAN`-gated sensing ingestion paths |
| F2 | Wildcard CORS | ✅ | PR #79 (`b7d5bc49`) |
| F3 | `/api/system/exec` RCE | ➖ | 2-layer defense locked: PR #69 nginx `location = /api/system/exec` `allow 127.0.0.1; deny all;` + PR #81 Go `localOnlyMiddleware` re-checks `RemoteAddr` / `X-Forwarded-For` / `X-Real-IP` for loopback. **Decision skip "remove"** (locked 2026-05-20): the OpenClaw agent on-device legitimately uses exec for debug; any caller reaching loopback already has root anyway under the shared-secret threat model, so removing the endpoint subtracts the agent feature without adding protection. Command-whitelist + admin-auth-on-top were considered (options B + C) and rejected — effort exceeds ROI given the threat model |
| F4 | `/api/system/shell` | ✅ | 2026-05-20 Login UI batch: `system.GET("shell")` gated by `adminAuthMiddleware` — browser WebSockets carry the `lamp_session` cookie automatically. Scripts can still pass `?token=<llm_api_key>` since WS upgrade can't set Bearer headers in browsers |
| F5 | `/api/openclaw/config-json` raw config | ✅ | 2026-05-20: the front-end no longer fetches it (Login UI batch dropped `monitor/index.tsx::AgentGWMenu` token fetch + `GwConfig.tsx` raw render + `ChatSection.tsx` model label). The endpoint stays `localOnlyMiddleware`-gated. The gateway link drops the `#token=` fragment — the on-device browser OpenClaw control UI handles its own auth |
| F6 | `GET /api/device/config` secret dump | ✅ | 2026-05-20: New `domain.ConfigPublicResponse` returns booleans (`has_llm_api_key`, `has_*_token`, `has_*_password`) plus non-secret URLs / IDs. The raw `ConfigResponse` type + `device.Service.GetConfig` were deleted. The endpoint is gated by `adminAuthMiddleware` (cookie or Bearer) |
| F7 | `PUT /api/device/config` overwrite + side effects | ➖ | 2026-05-19: admin auth done (`adminAuthMiddleware` Bearer). 2026-05-20: URL validation + debounce **skipped** — the shared-secret design (`llm_api_key` = admin token) already accepts the "have key = root device" threat model. URL swap is just 1 of 6+ attacks anyone with the key could pull off (voice speak, camera, servo, OTA, …); validation is cosmetic, not a boundary. Debounce isn't needed since the web UI does 1 save = 1 PUT = 1 restart |
| F8a | `POST /api/device/setup` hijack | ✅ | 2026-05-20: `setupOrAdminMiddleware`. Pre-setup (SetUpCompleted=false) → open; post-setup → admin auth required (Bearer or cookie). Replaces the earlier strict `setupOnlyMiddleware` so `#force` re-setup works for admin-authed operators (e.g. existing devices migrating to the Login UI batch by setting admin_password) while still blocking unauthed re-setup |
| F8b | `POST /api/device/channel` hijack | ✅ | 2026-05-19: `adminAuthMiddleware` applied |
| F9 | Logs leak secrets | ✅ | 2026-05-19: admin auth. 2026-05-20: `redactLogLine()` regex scrubs 3 patterns (key=value secrets, `Authorization: Bearer`, bare `sk-...` keys) on file-based + journal tail + SSE stream + journal stream |
| F10 | `/api/system/software-update/:target` OTA trigger | ✅ | 2026-05-19: admin auth. 2026-05-20: per-target rate limit 30s (in-memory map + mutex), 429 with `Retry-After` header |
| F11 | Ingestion endpoints unauthenticated | ✅ | PR #81 — `sameOriginOrLAN` applied to mood/log, wellbeing/log, posture/log, music-suggestion/log+status, monitor/event, guard/alert. `sensing/event` per `a0ccfd23` |
| F12 | Lamp Go bind 0.0.0.0 | ✅ | PR #81 — bind `127.0.0.1:5000` |
| F13 | Bootstrap server bind 0.0.0.0 | ✅ | PR #81 — bind `127.0.0.1:8080` |

---

## Outstanding work — prioritized

### Quick wins (pure ops, ≤ 30 minutes, no runtime breakage)

_All cleared 2026-05-19 (F6 + F7b skipped + F8)._

### Frontend refactor (remaining after Login UI batch)

- [➖] **web F10** — central `apiFetch` wrapper SKIPPED 2026-05-20 (the interceptor in `lib/api.ts` covers the security need; refactor is cosmetic-only, ~65 raw-fetch sites for no security delta)
- [x] **web F12** — `/hw/docs` iframe rewired through admin-auth proxy + new `/openapi.json` route (2026-05-20)
- [x] **web F13** — `/api/voice/preview` Go endpoint (2026-05-20)

### Defense-in-depth follow-ups (logged from 2026-05-20 work)

- [x] **CSP `'unsafe-inline'` regret** — RESOLVED 2026-05-20. HAL now ships its own Swagger UI bundle (`os/hal/static/`), a custom `/docs` handler with no inline `<script>`, and an external `swagger-init.js`. Nginx CSP reverted to `script-src 'self'` (no `'unsafe-inline'`, no `cdn.jsdelivr.net`).
- [x] **CDN whitelist** — RESOLVED 2026-05-20 in the same change (no CDN reference left in the CSP).

### Backend auth (Login UI batch closed go F4 / F5 / F6; remaining bullets)

- [x] **go F1** — `adminAuthMiddleware` accepts Bearer OR `lamp_session` cookie. Applied to: GET/PUT device/config, POST device/channel, POST system/software-update, GET logs/tail+stream, GET system/shell. The web sets the cookie via `POST /api/login` (bcrypt verifies `admin_password_hash`)
- [x] **go F6** — `ConfigPublicResponse` sanitized; old raw type deleted (2026-05-20 Login UI batch)
- [x] **go F8b** — `POST /api/device/channel` admin auth ✅
- [x] **go F9** — Logs redact regex (`redactLogLine()` on file / journal / SSE)
- [x] **go F10** — `/api/system/software-update/:target` per-target 30s rate limit

### Decision items (locked 2026-05-20)

- [x] **web F6** — CliSection: **decision locked accept-as-is**. Backend admin-gated (F4 closed) + sidebar `debug=true` gate + WS reject on missing auth = 3-layer defense. Bundle-size strip via `import.meta.env.DEV` rejected (cosmetic, no security delta).
- [x] **go F3** — `/api/system/exec`: **decision locked skip-remove**. 2-layer defense (nginx deny LAN + Go localOnly) sufficient under the shared-secret threat model. The OpenClaw agent on-device uses it; removing it would subtract a feature without adding protection.

### Patch script idempotency note

`scripts/maintenance/patch-security.sh` now hashes `lamp.conf` + `hal.service` before patching and only `nginx -s reload` / `systemctl restart` when those hashes change. Earlier behavior was an unconditional restart at the end → re-running an already-patched device caused a ~5s 502 window. Safe to re-run repeatedly now.

---

## Coverage summary

| Audit | Done | Partial | Not done | Skipped | Total |
|---|---|---|---|---|---|
| Local-only | 11 | 0 | 0 | 1 (F7b out-of-scope) | 12 |
| Frontend | 13 | 0 | 0 | 1 (F10 cosmetic refactor) | 14 |
| Go server | 12 | 0 | 0 | 2 (F7 + F3 shared-secret trade-offs) | 14 |
| **Total** | **36** | **0** | **0** | **4** | **40** |

**90% done**, **0% partial**, **0% outstanding**, **10% accepted-skipped**. Audit fully closed: every finding is either ✅ (fix shipped) or ➖ (decision locked under the shared-secret threat model). No active decision items remain.

Day-by-day 2026-05-20 batches:
- **Login UI batch** — closed 9 items: web F1/F2/F3/F4/F14, local F5b, go F4/F5/F6. Cookie-based auth (`lamp_session` HMAC) + bcrypted admin password + `ConfigPublicResponse` is now the canonical browser entry; Bearer is kept as a fallback for scripts. Re-setup via `#force` works on already-provisioned devices through the hybrid `setupOrAdminMiddleware` (pre-setup open, post-setup admin-gated).
- **Web F13** — TTS preview routed through `POST /api/voice/preview` (Go reads the TTS key server-side); the browser body carries `{text, voice, provider}` only.
- **Web F12 (with F9 trade-off → reverted)** — `/hw/docs` iframe now loads via `/api/hardware/docs` (Go reverse proxy, admin-auth gated). New `/openapi.json` route (Go + nginx location) returns the HAL spec through the same auth gate. The initial CSP loosening (`cdn.jsdelivr.net` + `'unsafe-inline'` script-src) needed for FastAPI's auto-generated Swagger HTML was reverted later the same day by self-hosting Swagger assets in HAL; CSP is now back to strict.
- **Go F1 / F3 / web F6 closeout** — gated every `/api/openclaw/*` endpoint with admin auth (F1 → ✅), locked the `/api/system/exec` 2-layer-defense decision as skip-remove (F3 → ➖), and locked the CliSection 3-layer-defense decision as accept-as-is (web F6 → ✅).
