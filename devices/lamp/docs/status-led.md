# Status LED — Specification

Status LEDs give users visual feedback about what Lamp is doing internally.
Without them, users cannot tell whether Lamp is booting, updating, disconnected from its AI brain, or otherwise impaired.

## Design Principles

1. **Glanceable** — each state has a unique color so the user never has to guess.
2. **Non-intrusive** — status LEDs yield to user-initiated scenes/emotions. When the status clears, the strip is restored to whatever the user (or agent) last set, and ambient resumes after silence.
3. **Priority-based** — when multiple states are active simultaneously, the highest-priority state wins.

## States

All states use the `breathing` effect at speed 3.0 unless noted. RGB values come from `internal/statusled/service.go`.

| State (code constant) | Color | RGB | Meaning | Triggered by | Auto-clears |
|---|---|---|---|---|---|
| `StateConnectivity` | Orange | `(255, 80, 0)` | **No Internet** — Wi-Fi connected but no internet | Network monitor: 5 consecutive ping failures (~25s) | Yes — when ping succeeds |
| `StateError` | Red | `(255, 0, 0)` | **Error** — System error (reserved) | Critical failure | Yes — when error resolves |
| `StateOTA` | Green | `(0, 255, 0)` | **Updating** — OTA firmware update in progress (reserved enum; bootstrap drives OTA LED directly via `lib/hal` — see "Bootstrap (OTA)" below) | Bootstrap reconcile detects update | Reboots after update completes |
| `StateBooting` | Blue | `(0, 80, 255)` | **Booting** — Lamp is starting up | `server.go` on startup | Yes — when OpenClaw agent connects and is ready |
| `StateLeLampDown` | Purple | `(180, 0, 255)` | **HAL Down** — Hardware server unreachable. While HAL is down the LED is **dark** because the LED driver itself is down; the purple breathing only shows for ~3s on recovery | `healthwatch` poll fails to reach HAL `/health` | Auto-clears 3s after recovery |
| `StateAgentDown` | Cyan | `(0, 200, 200)` | **Agent Down** — AI brain disconnected | OpenClaw WebSocket drops (`internal/openclaw/service_ws.go`) | Yes — when WebSocket reconnects |
| `StateHardware` | Yellow | `(255, 255, 0)` | **Hardware Failure** — servo/LED/audio/voice component reports unhealthy via HAL `/health` | `healthwatch` poll (every 5s); camera and sensing excluded | Yes — when all monitored components report healthy |

### Ready flash

After boot completes (Booting cleared and no other state active), `statusled.FlashReady()` fires a brief **white** `notification_flash` for ~1s to indicate the agent is ready to accept commands. Suppressed if any status state is active.

### OTA sub-states (driven by bootstrap)

The bootstrap binary calls `lib/hal` directly (it does not go through `statusled.Service`):

| Phase | LED behavior | Source |
|---|---|---|
| Downloading + installing | Orange `(255, 140, 0)` `breathing` speed 0.4 | `bootstrap/bootstrap.go` |
| Success | Green `(0, 255, 80)` brief `notification_flash` then stop | `bootstrap/bootstrap.go` |
| Failure | Red `(255, 30, 30)` `pulse` speed 1.5 | `bootstrap/bootstrap.go` |

Note that bootstrap's OTA orange/red use slightly different RGB and effect parameters than the `statusled.Service` enum entries — bootstrap is a separate binary that owns the LED while OTA is in progress.

## Priority

When multiple `statusled.Service` states are active simultaneously, the highest-priority state is shown:

```
Connectivity (highest) > Error > OTA > Booting > HAL Down > Agent Down > Hardware (lowest)
```

Priority numbers (from `priority` map in `service.go`):

| State | Priority |
|---|---|
| `StateConnectivity` | 7 (highest) |
| `StateError` | 6 |
| `StateOTA` | 5 |
| `StateBooting` | 4 |
| `StateLeLampDown` | 3 |
| `StateAgentDown` | 2 |
| `StateHardware` | 1 (lowest) |

Example: if Lamp has no internet AND the agent is down, **No Internet** (orange) wins because it has higher priority.

Bootstrap's OTA LED writes bypass this priority queue — they run while bootstrap owns the strip, typically when lamp is being restarted.

## Behavior Details

### Booting (Blue)
- Activated by `server.go` at startup, before the agent is ready
- Cleared when the OpenClaw agent connects and is ready to accept commands
- Followed by a brief white `FlashReady` flash to signal "ready to listen"

### Connectivity / No Internet (Orange)
- Network service pings every 5 seconds
- After 5 consecutive failures (~25 seconds), `StateConnectivity` is set
- Cleared immediately when a ping succeeds
- Lamp continues to function locally but cloud features are unavailable

### Agent Down (Cyan)
- Activated when the OpenClaw WebSocket connection drops
- Cleared when the WebSocket reconnects successfully
- Voice commands and AI features are unavailable; local LED scenes and servo still work
- TTS announces "Brain reconnected!" on recovery

### HAL Down (Purple — or dark/black)
- When HAL crashes the LED goes **dark** because the LED driver itself is down
- `healthwatch` polls every 5 seconds and tracks the outage
- On recovery, purple breathing flashes for ~3s as the state clears, then normal LED resumes
- TTS announces "Hardware recovered!" on recovery
- LED control, servo, camera, mic, and speaker are all unavailable while HAL is down

### Hardware Failure (Yellow)
- Activated when servo, LED driver, audio, or voice pipeline reports unhealthy via HAL `/health`
- Per-servo online check via `lelamp.GetServoStatus()` — any offline servo trips it
- Camera and sensing are excluded (may be intentionally off by scene preset)
- Health watcher polls every 5 seconds
- Cleared automatically when all monitored components report healthy
- Check the web monitor for specific component details

### OTA Update (Green / Orange / Red — bootstrap)
- See "OTA sub-states (driven by bootstrap)" above
- Device reboots after a successful update — LED transitions to Booting (blue) on the new boot

### Error (Red — reserved)
- `StateError` enum is defined in `statusled.Service` but is not currently set by any caller in lamp
- Bootstrap uses red `pulse` directly to indicate OTA failure (not via `statusled.Service`)

## Architecture

### Lamp (os-server)

`internal/statusled/Service` manages active states with a priority map. Callers `Set` and `Clear` named states; the service applies the LED effect for the highest-priority active state.

Concrete callers (verified against code):

```
server.go                    → Set/Clear StateBooting + StateConnectivity + FlashReady
internal/openclaw/service_ws → Set/Clear StateAgentDown
internal/healthwatch/service → Set/Clear StateLeLampDown + StateHardware
```

The service calls HAL's `/led/effect` endpoint via `lib/hal` (shared HTTP client).

### Bootstrap (bootstrap-server)

Bootstrap is a separate binary. It calls `lib/hal` **directly** in the `reconcile` function (not through `statusled.Service`):

```
reconcile detects update → lelamp.SetEffect("breathing", 255, 140, 0, 0.4)   // orange
        ↓ applies update...
success → lelamp.SetEffect("notification_flash", 0, 255, 80, 1.0)            // green flash
failure → lelamp.SetEffect("pulse", 255, 30, 30, 1.5)                        // red pulse
```

## Integration with Ambient

The ambient service (`internal/ambient`) pauses on interaction events (`chat_send`, `chat_response`, etc.). When `statusled.Service` clears the last active state, it calls `lelamp.RestoreLED()`, which hands the strip back to whatever color/effect the user (or agent) last set via `/led/solid`, `/led/effect`, or `/scene`. If no user state exists, the strip clears to off and ambient resumes its breathing LED after 60s of silence.

All `statusled.Service` writes use `transient=true` so they do not clobber the user's saved LED state — emotion's restore-after-animation reads back the user's color, not the status color. (Bootstrap's direct `lib/hal` calls are also transient.)

## Shared HAL Client

`lib/hal/client.go` provides a thin HTTP wrapper used by all Go code that controls LEDs:

| Function | Endpoint | Purpose |
|---|---|---|
| `SetEffect(effect, r, g, b, speed)` | `POST /led/effect` (transient) | Start a named effect — does not save user LED state |
| `StopEffect()` | `POST /led/effect/stop` | Stop running effect |
| `RestoreLED()` | `POST /led/restore` | Hand strip back to user's saved state |
| `SetSolid(r, g, b)` | `POST /led/solid` | Set solid color |
| `Off()` | `POST /led/off` | Turn off LEDs |

All calls are fire-and-forget with a 5s timeout. Hardware unavailability is silently ignored.

## Normal Operation

When no status state is active, the LED is controlled by:

1. **Emotion presets** — colors driven by the AI agent's emotional state (see [emotion-led-mapping.md](emotion-led-mapping.md))
2. **Scene presets** — user-selected lighting scenes (reading, focus, relax, etc.)
3. **Ambient breathing** — gentle warm breathing when idle

A status state **overrides** all of the above when active. Once it clears, normal LED behavior resumes automatically.

## User Experience

| User sees | What's happening |
|---|---|
| Blue breathing | Lamp is booting |
| Brief white flash | Lamp is ready to listen |
| Cyan breathing | AI brain is disconnected (Lamp can still control lights/servo locally) |
| Purple breathing (after dark) | HAL recovered from a crash |
| Dark / no LED | HAL crashed (LED driver is down) |
| Orange breathing | No internet (Lamp is offline) |
| Yellow breathing | A hardware component is unhealthy |
| Green breathing | OTA firmware update in progress |
| Green flash | OTA update completed successfully |
| Red pulse | OTA update failed |
| Warm breathing (normal) | Lamp is idle, just vibing |
