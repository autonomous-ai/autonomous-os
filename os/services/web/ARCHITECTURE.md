# Web Frontend Architecture (`os/services/web`)

Single-page app served by the Go `os-server`. It is the device's local control
panel: it onboards the device (WiFi / AI brain / channels), lets the operator
edit that config later, and gives a live **Monitor** dashboard over everything
the device is doing (hardware, sensing, the agent turn pipeline, logs, a shell).

- **Stack:** React 19 + TypeScript + Vite 7 + Tailwind CSS 4 + Radix UI (shadcn-style `components/ui`), React Router 7.
- **Charts:** Chart.js / react-chartjs-2. **Terminal:** xterm.js. **Toasts:** sonner. **QR:** qrcode.react.
- **Module name:** `lamp-web` (legacy name; env vars are prefixed `LAMP_`).
- **No test framework is currently installed** — see [Testing](#5-how-to-test-this-easily) for how to exercise it anyway.

---

## 1. Functional areas (what it does)

| Area | Route | Purpose |
|------|-------|---------|
| **Setup wizard** | `/setup` | First-boot onboarding: device ID, admin password, WiFi, (debug) AI brain / channels / language / voice. Has a "continue" mode for voice + face enrollment. |
| **Login** | `/login` | Password → `os_session` httpOnly cookie. Gates the authed routes. |
| **Monitor dashboard** | `/monitor` | Live operational view of the running device (default landing page). |
| **Edit config** | `/edit` | Post-setup config editing (same sections as Setup, dirty-only save). |
| **Gateway config** | `/gw-config` | Read-only viewer for the agent's `openclaw.json` (now loopback-gated; shows instructions). |

### Routing & guards (`src/App.tsx`, `src/main.tsx`)

| Path | Component | Guard |
|------|-----------|-------|
| `/` | RootRedirect → `/monitor` | `AuthGate` |
| `/login` | `Login` | public |
| `/setup` | `Setup` | `SetupGate` (provisioning state) |
| `/monitor` | `Monitor` | `AuthGate` |
| `/edit` | `EditConfig` | `AuthGate` |
| `/gw-config` | `GwConfig` | `AuthGate` |
| `/dashboard` | → `/monitor` | redirect |

**`AuthGate`** probes `GET /api/device/config`: `200` → authed; `401` →
`/login?next=<path>`; `503` (no admin yet) → `/setup`.

---

## 2. Data flow & backend contract

### Two API namespaces

All requests go through the same origin (the device). In dev they are proxied to
a real device via `LAMP_PROXY` (`vite.config.ts`), with `ws: true` so WebSockets
upgrade through the proxy.

- **`/api/*`** — os-server (Go/Gin) control plane: config, setup, login, voice, agent flow, logs, analytics, PTY shell.
- **`/api/hardware/*`** (built by `hwUrl()` in `App.tsx`) — proxied to the **HAL** (Python/FastAPI on `:5001`): servo, LED, camera, sensing, face, bluetooth, audio. The monitor agents often refer to these as `/hw/*` (same thing after the `/api/hardware` prefix).

### Response envelope

Every endpoint returns `{"status": 1, "data": <payload>, "message": null}` on
success / `{"status": 0, "data": null, "message": "error"}` on failure.
`apiRequest()` in `src/lib/api.ts` unwraps `.data`; `camelcase-keys` normalizes
snake_case → camelCase.

### Auth / session model

- `POST /api/login` with the admin password → server sets an httpOnly
  `os_session` cookie (`SameSite=Strict`). No token in JS for normal use.
- A patched `window.fetch` (`App.tsx`) forces `credentials: "include"` on every
  `/api/*` call and attaches a legacy `Bearer` token from `sessionStorage`
  (`lamp_api_token`) **if present** — used only by scripted/URL-seeded callers.
- URL-borne secrets (`?llm_api_key=…`, passwords) are picked up, exchanged for a
  cookie via `POST /api/login/exchange`, then **scrubbed from the address bar**
  (`scrubLocationSecrets()`).

### Three real-time transports

| Transport | Hook / mechanism | Used by |
|-----------|------------------|---------|
| **Polling** | `usePolling` (in-flight guard, 4s abort timeout, pauses on hidden tab) | Overview/System (5s), Camera/Sensing/Servo/Bluetooth/Face (2s), Logs (1–2s), Analytics (on range change) |
| **SSE (EventSource)** | `useEventSource` (`withCredentials`, pauses on hidden tab) | **Flow Monitor** + **Chat** → `/api/agent/flow-stream` (turn pipeline events, capped ~10K in memory) |
| **WebSocket** | raw `WebSocket` | **CLI** xterm PTY (`/api/pty/new`, `/api/pty/{id}/ws`), **Camera** MJPEG stream (`hwUrl("/camera/stream")`) |

### API surface (grouped, from `src/lib/api.ts`)

- **Network/Setup:** `GET /api/network`, `POST /api/network/setup`, `GET /api/network/check-internet`, `POST /api/device/setup`, `GET /api/device/setup/status` (open — used for AP→STA redirect).
- **Config:** `GET /api/device/config` (admin-gated; secrets returned only as `has_*` booleans), `PUT /api/device/config` (partial; secrets sent only when typed).
- **Auth:** `POST /api/login`, `POST /api/logout`, `POST /api/login/exchange`.
- **TTS/Voice:** `GET /api/device/tts-providers`, `GET /api/device/voices?provider&lang`, `POST /api/voice/preview`.
- **Hardware (`/api/hardware/*`):** servo, led, camera, sensing, face (enroll/remove/owners), bluetooth, audio, presence, scene, display.
- **Agent/observability:** `/api/agent/flow-stream` (SSE), `/api/analytics`, `/api/logs?source=&limit=`, `/api/pty/*` (WS).

---

## 3. Setup flow (data flow detail)

```
URL params (useSetupUrlParams, snapshotted before scrub)
        │
        ▼
useConfigPrefill ──GET /api/device/config──▶ hydrate form (non-secret fields + has_* flags)
        │
        ▼
Single flat form state in Setup.tsx  ◀── section components push via callbacks
        │  (submit)
        ▼
setupDevice(body) ──POST /api/device/setup──▶ device begins switching AP→STA
        │
        ▼
useSetupStatusPolling: 3 parallel pollers
   ├─ phase poll  GET /api/device/setup/status (2s) → connecting|connected|failed
   ├─ LAN IP probe (health check once IP known)
   └─ mDNS probe  lamp-XXXX.local health check
        │
        ▼
on "connected": redirect to canonical URL (.local) / show "device online"
```

**Sections (order):** Device → WiFi → (debug: AI Brain, Channels, Language,
Voice) → (continue mode: My Voice enroll, Face enroll).
Secrets are never prefilled — the form shows a "configured" lock and only sends
a field when the operator actually types into it.

**Setup hooks (`src/hooks/setup/`):**
- `types.ts` — shared union/flag types (`SectionId`, `*LoadedState`).
- `useSetupUrlParams.ts` — module-level snapshot of query params *before* App scrubs secrets.
- `useConfigPrefill.ts` — hydrate form from `/api/device/config`; surface `has_admin_password` / `has_network_password`.
- `useSetupStatusPolling.ts` — the 3-poller connect/redirect logic above.
- `useTTSCatalog.ts` — load providers + voices, refetch on provider/language change, validate prefilled selections.
- `useFaceEnroll.ts` — face enroll/remove/list against `/api/hardware/face/*`.

**EditConfig** mirrors Setup's sections but is dirty-tracked (compares against an
`initialRef` baseline) and warns that AI Brain / channel changes need a reboot.

---

## 4. Monitor dashboard (data flow detail)

`src/pages/monitor/index.tsx` is the shell; each tab is a `*Section`. Chat is
always mounted (to preserve thread context); some tabs are debug-only.

| Section | Shows | Source |
|---------|-------|--------|
| Overview | health (CPU/RAM/uptime), network, hw status, presence, scene, LED color | poll 5s `/system/*` + several `/hw/*` |
| System | rolling 60-pt CPU/RAM/disk/temp history, goroutines, uptime breakdown | poll `/system/info` |
| **Flow** | **turn pipeline** input→intent→(local/agent/queued/dropped)→hw output→channel | **SSE `/api/agent/flow-stream`** |
| Camera | live MJPEG, snapshot, track status, zoom/FPS | poll 2s `/hw/camera` + WS stream |
| Sensing | pose/ergo, light, audio, motion, environment, risk levels | poll 2s `/hw/sensing/*` |
| Servo | joint angles, online/error, manual move, animation replay | poll 2s `/hw/servo*` + POST move |
| Bluetooth | paired devices, scan/pair/connect | poll 2s `/hw/bluetooth/*` |
| Face-Owners | enrolled faces, galleries, cooldown timers, per-user history | poll 2s `/hw/face/*` |
| Chat | web chat UI, history, quick-pick | SSE flow-stream + RPC to agent |
| CLI *(debug)* | multi-tab xterm terminal, PTY per tab | **WebSocket** `/api/pty/{id}/ws` |
| Logs *(debug)* | aggregated HAL / os-server / agent / buddy logs, level + ANSI strip | poll 1–2s `/api/logs?source=` |
| Analytics *(debug)* | turn count, duration P50/P95, token metrics, inner-loop timing | poll `/api/analytics?from&to` |

**Flow Monitor** (`monitor/FlowSection/`) groups SSE events into **Turns**
(keyed by runId, with path + status) and renders a `FlowDiagram` SVG of ~25
pipeline nodes, highlighting the active/visited stages. Modals: `CanvasModal`
(fullscreen diagram), `CompactionModal` (agent context compaction stats),
`PipelineModal` (mobile turn card), `PoseBucketModal` (ergo pose frames),
`UserTimelineModal` (per-user mood/wellbeing history), `TurnBadge` (turn summary).

---

## 5. How to test this easily

There is **no test runner installed** today. Easiest → most thorough:

### A. Run the UI against a real device (fastest, recommended)
The dev proxy forwards `/api` and `/hw` (with WS upgrade) to a device, so you get
real data with hot reload and no device-side rebuild:
```bash
cd os/services/web
echo "LAMP_PROXY=http://<DEVICE_IP>" > .env
npm install
npm run dev          # http://localhost:5173, proxied to the Pi
```
Log in with the device admin password. Now you can exercise Setup, Edit, and
every Monitor tab (SSE Flow, WS CLI, camera stream all proxy through).

### B. Type-check + lint (no runtime, catches most regressions)
```bash
npm run build        # tsc -b + vite build — fails on any TS error
npm run lint         # eslint
```

### C. Mock the backend (UI work with no device)
Because every call goes through `src/lib/api.ts` and the `{status,data,message}`
envelope, the cleanest seam is to point the proxy at a tiny mock server that
returns that envelope, **or** stub `apiRequest`/the patched `window.fetch`.
Useful fixtures to fake:
- `GET /api/device/config` → `{has_admin_password:true, ...}` to land on Monitor.
- `GET /api/device/setup/status` → `{phase:"connected", lan_ip, mac}` to drive the Setup wizard end state.
- `/api/agent/flow-stream` → an SSE endpoint replaying recorded turn events to exercise Flow Monitor without a live agent.

### D. Add a real test layer (recommended next step)
Stack fits naturally:
- **Vitest + React Testing Library** for hooks/components. Highest-value targets are the pure-ish hooks: `usePolling`, `useEventSource`, `useSetupStatusPolling`, `useTTSCatalog`, `useConfigPrefill` — mock `fetch`/`EventSource` and assert state transitions.
- **MSW (Mock Service Worker)** to intercept `/api/*` with the response envelope — gives both tests and option C above from one set of handlers.
- **Playwright** for the two flows that matter most end-to-end: the Setup wizard (prefill → submit → connected redirect) and AuthGate redirects (401→login, 503→setup).

**Why these are the easy wins:** the app is cleanly layered — one API module,
one envelope shape, three transports behind three hooks. Faking the transport
layer (fetch / EventSource / WebSocket) exercises almost all logic without
hardware.

---

## 6. File map (orientation)

```
src/
  main.tsx, App.tsx        # router, AuthGate/SetupGate, patched fetch, hwUrl()
  lib/api.ts               # the API client + response-envelope unwrap
  hooks/
    usePolling.ts          # interval poll w/ in-flight guard + abort + visibility
    useEventSource.ts      # SSE w/ credentials + visibility pause
    useOnboardingComplete.ts
    setup/*                # the 6 setup hooks (see §3)
  pages/
    Login.tsx Setup.tsx EditConfig.tsx GwConfig.tsx
    monitor/               # dashboard shell + one file per tab + FlowSection/
  components/
    setup/*  edit/*        # config section components (initial vs post-setup)
    ui/*                   # shadcn/Radix primitives
```
