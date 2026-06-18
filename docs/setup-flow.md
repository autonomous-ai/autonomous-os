# Setup Flow — Documentation

## Overview

When the OS server is not yet configured (`SetUpCompleted = false`), the device runs in AP mode, serving a Web UI for user setup.

## Flow

```
1. Device boots → check config.json
2. Not set up → AP mode (WiFi hotspot)
3. User connects to WiFi → opens Web UI
4. Enters: WiFi SSID/password + LLM config + channel
5. POST /api/device/setup
6. OS Server processes (async):
   a. Connect WiFi (connect-wifi CLI) — in parallel, an early-capture
      goroutine publishes the STA LAN IP into setup state as soon as wlan0
      gets one (before internet is up), so the Web UI can read it while the
      AP is still briefly alive (see "AP→STA Auto-Redirect")
   b. Wait for internet (poll 60s)
   c. Setup agent gateway
   d. Save config
   e. Wait for agent ready (poll 120s)
   f. Report to backend (MQTT)
   g. SetUpCompleted = true
7. On failure → return to AP mode
8. Web UI auto-redirects the browser to http://<lan_ip>/setup once the
   operator is back on home Wi-Fi (mDNS .local used as a secondary channel)
```

## API

### POST /api/device/setup

```json
{
  "network_ssid": "MyWiFi",
  "network_password": "...",
  "llm_provider": "anthropic",
  "llm_api_key": "sk-...",
  "llm_base_url": "https://api.anthropic.com",
  "llm_model": "claude-haiku-4-5-20251001",
  "channel_type": "telegram",
  "channel_token": "...",
  "channel_id": "...",
  "mqtt_endpoint": "broker.example.com",
  "mqtt_port": 8883,
  "mqtt_username": "...",
  "mqtt_password": "...",
  "fa_channel": "fa/device123",
  "fd_channel": "fd/device123",
  "deepgram_api_key": "..."
}
```

**Response:** Returns immediately `{"status": 1}`. Setup runs async in a goroutine after 2s delay.

### POST /api/device/channel

Change messaging channel after setup is complete. Accepts `telegram`, `slack`, `discord`.

**WhatsApp is rejected here** (`400 whatsapp pairing not supported via HTTP; use MQTT add_channel`) — WhatsApp pairing streams a rotating QR back to the caller, which HTTP's fire-and-forget shape can't carry. The canonical path is the MQTT `add_channel` command (see `docs/mqtt.md`) which publishes one fd_channel message per pairing event. Re-pairing without re-bootstrapping uses the MQTT `whatsapp_pair` command.

## Network Setup

1. Call `connect-wifi` CLI tool with SSID + password
2. Poll checks:
   - SSID match? (`iwgetid`)
   - Internet OK? (`ping`)
3. Timeout 60s → fail
4. Success → save SSID + password to config

## AP Mode

- When not set up or setup fails → automatically switches to AP mode
- Device broadcasts WiFi hotspot
- Web UI serves setup page
- `SwitchToAPMode()` in `internal/network/service.go`
- **LED indicator:** once HTTP server is listening, if `SetUpCompleted == false` the OS server spawns a background goroutine (`waitAndPaintSetupReady` in `server/server.go`) that polls HAL `GET /health` once per second up to 30s. As soon as `health.led == true`, it fires `POST /led/solid` with `{"color":[255,255,255]}` to paint the strip solid white. The poll exists because os-server typically binds :5000 before HAL's FastAPI is up on :5001 (Python loads `rpi_ws281x`, SPI, audio, camera) — a fire-and-forget paint would silently drop on `connection refused`. White stays on until setup completes (agent flash + ambient repaint it). The booting blue-breathing still shows during init.
- **AP-mode LED suppression:** the openclaw WS reconnect loop (`internal/openclaw/service_ws.go`) skips `StateAgentDown` Set/Clear while `config.SetUpCompleted == false`, so the cyan disconnect overlay doesn't fight the setup-needed white during provisioning. WS still runs (`device.Setup` needs it ready to satisfy `WaitForAgentReady` before flipping `SetUpCompleted=true`), only the LED side-effect is gated.

## AP→STA Auto-Redirect (the "joining Wi-Fi…" screen)

After the operator submits, the Setup page shows a "Your device is joining
Wi-Fi…" screen and tries to **automatically redirect the browser to the
device's new home-Wi-Fi address** once it's reachable, so the operator doesn't
have to hunt for an IP. This section documents how that redirect works, why it
used to get stuck, and what changed.

### The hard constraint

The device has **one Wi-Fi radio (`wlan0`)**. During provisioning it runs as an
access point at `192.168.100.1`. To join the home network it must switch the
same radio AP→STA — which **tears the AP down**. The moment the AP dies, the
browser (still associated to the AP SSID) **loses all network access to the
device** until the operator manually rejoins their home Wi-Fi. There is no
window in which the browser is on both networks at once.

Consequence: the browser can only learn the device's new LAN IP **before** the
AP tears down, or via mDNS **after** the operator is back on home Wi-Fi.

### Three redirect channels (`useSetupStatusPolling.ts`)

1. **Phase poll** — polls `GET /api/device/setup/status` against the AP IP while
   the AP is alive. Reads `phase` + `lan_ip`. Goes dark the instant the AP tears
   down.
2. **LAN-IP probe** — once `lan_ip` is known, probes `http://<lan_ip>/api/health`
   from the browser; when it succeeds (operator is back on home Wi-Fi and the
   device is up) it redirects to `http://<lan_ip>/setup?<params>`. **Works on
   every LAN regardless of mDNS** — this is the reliable fallback.
3. **mDNS probe** — probes `http://<type>-<id>.local/api/health`. Redirects when
   the browser can resolve the `.local` name. **Fails silently when the router
   blocks mDNS multicast** (common on many home/office routers, and on Android
   Chrome which has no native mDNS).

### Root cause of the "stuck forever" bug

Two independent defects made the screen hang indefinitely even though the device
had successfully joined Wi-Fi:

1. **CSP blocked every cross-origin probe.** The device's nginx served
   `Content-Security-Policy: …; connect-src 'self' ws: wss:`. The Setup page is
   served from the AP IP, so `'self'` is `http://192.168.100.1`. Both the
   `lan_ip` probe (`http://172.x.x.x/…`) and the mDNS probe
   (`http://…​.local/…`) are a **different origin**, so the browser refused the
   `fetch` with *"Refused to connect because it violates the document's Content
   Security Policy"* — `mode: "no-cors"` does **not** bypass CSP. Every channel's
   probe died before leaving the browser.

2. **The LAN-IP channel almost never had a `lan_ip` to use.** `lan_ip` was only
   published into setup state *after* `SetupNetwork()` finished its **up-to-60s**
   wait for internet. But the AP tears down within ~2s of that call starting, so
   the phase poll went dark long before `lan_ip` existed → the LAN-IP channel
   stayed disabled → only the mDNS channel remained → and on mDNS-blocking
   networks that one can't resolve either. Result: **no channel ever fires.**

So on a router that blocks mDNS multicast (the exact field case), the page was
permanently stuck on "joining Wi-Fi…" despite a fully successful join.

### The fix

| Layer | Change | Why |
|-------|--------|-----|
| **CSP** (`imager/build*.sh`, `scripts/provision/setup.sh`, `scripts/maintenance/patch-security.sh`) | `connect-src 'self' ws: wss:` → `connect-src 'self' ws: wss: http:` | Lets the browser `fetch` the cross-origin LAN-IP and `.local` probes. `http:` (not `http://*.local`) is required because **CSP can't express an IP range** — a single `http:` token is the only way to allow `http://<any-lan-ip>/…`, so the fix is independent of the customer's subnet (`172.x`, `192.168.x`, `10.x`). |
| **Backend** (`internal/device/service.go`) | A goroutine polls `GetCurrentIP()` once per second **in parallel with** `SetupNetwork()` and publishes the STA IP into setup state the instant it appears (skipping the AP's own `192.168.100.1`), before the 60s internet wait completes. | Gives the FE the **largest possible window** to read `lan_ip` during the brief overlap where it's still polling the AP — so the LAN-IP channel actually has an IP to redirect to. A guard keeps an already-captured IP from being clobbered by a later empty read during AP teardown. |
| **Frontend** (`useSetupStatusPolling.ts`) | LAN-IP redirect now carries `pathname + search` and targets `http://<lan_ip>/setup?<params>`. | Keeps the OS-server-pushed params (`llm_api_key`, `device_id`, …) in scope on the new host, matching the mDNS channel. The IP is read dynamically from the backend — **no hardcoded subnet**. |
| **Frontend** (`Setup.tsx`) | The "save this address" copy field now shows the **raw-IP URL** (`http://<lan_ip>/setup`), falling back to `.local` only until the early `lan_ip` poll lands. | The IP resolves on every LAN; the `.local` name fails on mDNS-blocking routers — so the manual safety-net link must prefer the IP. |
| **Frontend** (`Setup.tsx`) | The Copy button gained a `document.execCommand("copy")` fallback (hidden textarea) for when `navigator.clipboard` is unavailable. | The Setup page is served over plain HTTP (`http://192.168.100.1`), where `navigator.clipboard` is `undefined` (it needs a secure context) — so the modern API silently no-op'd and the button did nothing. The legacy path works on `http://` origins. |

### Redirect target

The happy path now redirects to **`http://<lan_ip>/setup?<params>`** (e.g.
`http://172.168.20.145/setup?…`) — the raw IP, which works regardless of mDNS.
Until the early `lan_ip` poll lands, the manual copy link falls back to
`http://<type>-<id>.local/setup?<params>`.

### Assessment & trade-offs

- **What it fixes:** the auto-redirect (and the manual copy link) now work on
  mDNS-blocking networks, which was the reported field failure. The solution is
  **subnet-agnostic** — it doesn't assume any particular private IP range.
- **What it still depends on:** the auto-redirect via the LAN-IP channel only
  fires if the FE captured `lan_ip` during the ~2s AP-alive overlap. The
  early-capture goroutine maximizes that window but can't guarantee it on a slow
  DHCP lease. The **manual IP copy link is the guaranteed fallback** — it's
  always shown and always uses the raw IP, so the operator is never stranded.
- **Security trade-off of `http:` in CSP:** `connect-src http:` permits the
  Setup page to `fetch` any plaintext-HTTP origin, not just the device.
  Acceptable here because the Setup bundle is served only on the LAN/AP, ships
  no secrets in these health probes, and CSP offers no narrower way to allow an
  arbitrary LAN IP. Tracked in `docs/security/CHECKLIST.md` (F9).
- **Why not avoid the AP teardown entirely:** a dual-radio or wired path would
  remove the constraint, but the target hardware has a single Wi-Fi radio — so
  the "learn the IP before AP dies, or via mDNS after" model is inherent.

## Post-Setup

After `SetUpCompleted = true`:
1. Connect OpenClaw WebSocket
2. Connect MQTT (subscribe fa_channel)
3. Start voice pipeline (if Deepgram key present)
4. Start ambient idle behaviors
5. Start sensing loop

## Config

Config stored at `config/config.json`. Managed by `server/config/config.go`.

| Field | Description |
|-------|-------------|
| `SetUpCompleted` | `true` when setup is done |
| `NetworkSSID` | WiFi SSID |
| `NetworkPassword` | WiFi password |
| `LLMProvider` | anthropic, openai, google, ... |
| `LLMApiKey` | LLM API key |
| `LLMBaseUrl` | LLM API base URL |
| `LLMModel` | Model name |
| `ChannelType` | telegram, slack |
| `ChannelToken` | Channel bot token |
| `ChannelID` | Channel/chat ID |
| `DeepgramApiKey` | Deepgram STT API key |
| `LocalIntent` | Enable/disable local intent matching (default: true) |
| `MQTTEndpoint` | MQTT broker host |
| `MQTTPort` | MQTT broker port |
| `FAChannel` | MQTT subscribe topic (server→device) |
| `FDChannel` | MQTT publish topic (device→server) |

## Parent-window event bridge

When the Setup page is opened as a popup/iframe from another site (e.g.
`autonomous.ai`), it reports each milestone back to the opener via
`window.postMessage`. This is the only cross-origin channel that works
popup→opener, since Setup is served from the device's AP IP
(`http://192.168.100.1`) or its `<type>-<id>.local` host — a different origin.

The opener should pass its origin so the device knows where to post and the
payload isn't broadcast to `*`:

```js
const origin = encodeURIComponent(window.location.origin);
window.open(`http://192.168.100.1/setup?parent_origin=${origin}&...`, "_blank");
```

Origin resolution order: `?parent_origin=` → `document.referrer` origin → `*`.

Every message is a flat JSON envelope:
`{ source: "autonomous-device-setup", v: 1, event, ts, ...data }`. Filter on
`source` and switch on `event`:

| `event` | When | Extra fields |
|---------|------|--------------|
| `setup_opened` | Wizard mounted | `mode`, `deviceId`, `mac` |
| `step_changed` | Operator changed wizard step | `step` |
| `wifi_selected` | A WiFi network was chosen | `ssid` |
| `setup_submitted` | "Setup" clicked, request about to send | `ssid`, `channel` |
| `setup_error` | Validation/backend error surfaced | `message` |
| `setup_connecting` | Device is joining WiFi (post-submit) | — |
| `setup_connected` | Device online + reachable | `mdns_host`, `lan_ip` |
| `setup_failed` | WiFi join failed | `message` |
| `retry_clicked` | "Back to Wi-Fi" after a failure | — |
| `continue_clicked` | "Continue setup →" clicked | `mdns_host` |
| `monitor_clicked` | "Go to monitor →" clicked | — |

Emits are best-effort: with no opener/parent they're a no-op, and postMessage
failures are swallowed, so the bridge never affects the setup flow itself. A
full listener example lives in the file header of `lib/setupBridge.ts`.

## Code

| File | Role |
|------|------|
| `os/services/internal/device/service.go` | Setup orchestration + early LAN-IP capture goroutine |
| `os/services/web/src/lib/setupBridge.ts` | Parent-window event bridge (postMessage) |
| `os/services/web/src/pages/Setup.tsx` | Setup wizard UI + bridge emit call sites + IP-first copy link |
| `os/services/web/src/hooks/setup/useSetupStatusPolling.ts` | AP→STA auto-redirect: phase poll + LAN-IP probe + mDNS probe |
| `os/services/internal/network/service.go` | WiFi connect, AP mode |
| `os/services/server/device/delivery/http/handler.go` | HTTP setup handler (async goroutine) |
| `os/services/server/config/config.go` | Config load/save |
| `imager/build-orangepi.sh`, `imager/build.sh`, `scripts/provision/setup.sh` | nginx config baked into the image (incl. `connect-src` CSP) |
| `scripts/maintenance/patch-security.sh` | OTA security patch for already-provisioned devices (migrates CSP) |
