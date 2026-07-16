# LED Control — Documentation

## Hardware

- **64 WS2812 RGB LEDs** — grid 8x5
- Driver: `rpi_ws281x` (Python, HAL owns)
- FastAPI endpoints on `:5001`

## Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/led` | LED strip info (count, available) |
| GET | `/led/color` | Current color `{"r", "g", "b"}` |
| POST | `/led/solid` | Fill entire strip with one color |
| POST | `/led/paint` | Set per-pixel colors (array up to 64 items) |
| POST | `/led/off` | Turn off all LEDs |
| POST | `/led/effect` | Start an effect |
| POST | `/led/effect/stop` | Stop running effect |
| POST | `/led/restore` | Repaint user's saved LED state (or clear if none) |

### Transient writes

`/led/solid`, `/led/effect`, and `/led/off` accept an optional `"transient": true` flag. When set, the call paints the strip but does **not** overwrite the saved user LED state. The saved state is restored when the caller (e.g. Claude Desktop Buddy) is done — either via the natural emotion restore timer, or by an explicit `POST /led/restore`. Pulse effects launched with `transient: true` also overlay on the user's saved color instead of black.

## Solid Color

```json
POST /led/solid
{"r": 255, "g": 180, "b": 100}
```

RGB values 0-255.

## Paint (Per-Pixel)

```json
POST /led/paint
{"pixels": [{"i": 0, "r": 255, "g": 0, "b": 0}, {"i": 1, "r": 0, "g": 255, "b": 0}]}
```

`i` = pixel index (0-63).

## Effects

```json
POST /led/effect
{"effect": "breathing", "r": 255, "g": 100, "b": 50, "speed": 1.0}
```

| Effect | Description | Params |
|--------|-------------|--------|
| `breathing` | Sine-wave brightness up/down | r, g, b, speed |
| `candle` | Random flickering candle | r, g, b |
| `rainbow` | Hue rotation across strip | speed |
| `notification_flash` | Quick flash 3 times | r, g, b |
| `pulse` | Single pulse from center outward | r, g, b, speed |

## Lighting Scenes

```json
POST /scene
{"scene": "reading"}
```

Each scene controls **all peripherals** — not just LED, but also camera, mic, speaker, and servo.

Deactivate: `POST /scene/off` — clears active scene, restores idle LED, re-enables camera/speaker, releases servo hold.

The active scene **survives HAL service restarts** (OTA, deploy, crash): it is persisted to a boot-scoped sidecar (`/tmp/hal-scene-state.json`, keyed to the kernel `boot_id`) and re-activated automatically when HAL comes back up, so the agent's belief ("focus mode is on") stays in sync. A full device reboot intentionally starts scene-less. Transient LED calls (`/led/solid`, `/led/off`, `/led/effect` with `"transient": true`, e.g. the boot breathing effect) overlay the strip without exiting the active scene; only non-transient LED overrides clear it.

| Scene | Bright | Color (K) | Servo | Camera | Mic | Speaker |
|-------|--------|-----------|-------|--------|-----|---------|
| `reading` | 80% | 4000K warm white | desk + hold | off | on | off |
| `focus` | 70% | 4200K warm-neutral | desk + hold | off | on | off |
| `relax` | 40% | 2700K warm | wall | on | on | on |
| `movie` | 15% | 2400K dim amber | wall | off | on | off |
| `night` | 5% | 1800K deep amber | down | off | on | off |
| `energize` | 100% | 5000K daylight | up | on | on | on |

### Scene peripheral control

When a scene activates, `POST /scene` applies in order:

1. **LED** — solid color = `preset.color × preset.brightness`
2. **Servo aim** — moves lamp head to preset direction (desk, wall, up, down)
3. **Servo hold** — if `"servo": "hold"`, freezes servo **after** aim completes (aim → hold in one thread). Released when switching to a scene without hold.
4. **Camera** — auto on/off via `_auto_camera_on`/`_auto_camera_off`
5. **Mic** — mute stops voice pipeline (STT), unmute restarts it
6. **Speaker** — mute stops TTS + music playback, unmute re-enables output

### Emotion suppression during hold mode

When servo is in hold mode (reading/focus), **emotion animations are suppressed** to avoid distraction:

- `happy`, `thinking`, `curious`, `sad`, etc. → servo + LED skipped
- `greeting`, `sleepy`, `stretching` → **allowed** (these signal state changes: wake, sleep, scene transition) — **scene-preset holds only**

An **explicit `/servo/hold`** (agent command like "face the wall and stay there") sets `_hold_explicit` and suppresses the servo for **all** emotions, scene-change set included — a trailing `[HW:/emotion:greeting]` in the same reply used to ride the exemption and park the arm at the greeting pose instead of the commanded one. `/servo/resume` and scene transitions clear the flag.

This means during focus, sensing events (face emotion, motion) still reach OpenClaw but Lamp stays physically still and visually stable.

### Color temperature rationale

- **Focus 4200K/70%** (not 5000K/100%) — 4000-4300K optimizes alertness without visual fatigue for sustained work
- **Night 1800K deep amber** — blue-free wavelengths (>580nm) preserve melatonin production
- **Movie mic on** — allows voice control ("pause", "stop") while watching

## Status LED

See details: [status-led.md](status-led.md)

LED feedback for system states (all `breathing` at speed 3.0 unless noted):

| State | Color | RGB |
|-------|-------|-----|
| Connectivity (no internet) | Orange | `(255, 80, 0)` |
| Booting | Blue | `(0, 80, 255)` |
| HAL Down | Purple | `(180, 0, 255)` |
| Agent Down | Cyan | `(0, 200, 200)` |
| Hardware Failure | Yellow | `(255, 255, 0)` |
| OTA in progress (bootstrap) | Orange | `(255, 140, 0)` |
| OTA success (bootstrap) | Green flash | `(0, 255, 80)` |
| OTA failure (bootstrap) | Red pulse | `(255, 30, 30)` |

Managed by `internal/statusled/Service` (lamp) and `lib/hal` directly (bootstrap).

None of these colors are hardcoded in Go anymore — `internal/statusled` states, the
bootstrap OTA-progress colors, and the setup-needed white all flow through HAL. The OS
owns the state machine (WHEN a state shows) and sends the state *name* to HAL
(`POST /led/status`: booting/error/ota/connectivity/hal_down/agent_down/hardware/
ready_flash/ota_progress/ota_error/ota_success/setup); HAL resolves the color/effect/speed
from `STATUS_LED_PRESETS`, overridable per device via `presets.json`'s `status_led` section
(see [DEVICE-SPEC.md § Per-device presets](../../../contract/DEVICE-SPEC.md#per-device-presets-presetsjson)).
`setup` is a persistent solid (saved as the displayed state); the rest are transient overlays.

### Mic-muted idle indicator

`STATUS_LED_PRESETS["mic_muted"]` — dark red `(140, 0, 0)` breathing at speed 0.8. HAL-local
key (no Go statusled state): applied by `POST /voice/mute`, cleared by `POST /voice/unmute`
(`app_state._mic_muted_led`). It is the strip's **resting look** while the mic is muted —
nothing is blocked:

- Emotions, effects, TTS/music waves, and transient overlays all run normally on top.
  When they finish, every LED restore (`_restore_user_led`, `POST /led/restore`) settles
  back on the red instead of the user state — "nothing happening + red breathing" means
  the mic is muted.
- An explicit user LED command (non-transient `/led/solid|off|effect`, `/led/paint`)
  dismisses the indicator — the user's ask wins the strip; the mic stays muted.
- Yields to deliberate lighting choices: user LED-off stays dark, an active scene keeps
  its functional lighting (the flag persists, so leaving the scene while still muted
  brings the red back on the next restore). Scene mic-unmute paths (`/scene` with
  `mic:"on"`, `/scene/off`) also clear it.
- `_user_led_state` is never touched — unmute restores the user's saved look.

### Setup-needed solid (lamp)

When lamp starts and `config.SetUpCompleted == false` (device in AP/provisioning mode), `server/server.go` spawns a background goroutine that polls HAL `GET /health` once per second up to 30s, and once `health.led == true` fires `lelamp.SetSolid(255, 255, 255)` — paints the strip solid white as a "device ready, connect to my hotspot" cue. Polling (not a single call) handles the cold-boot race where os-server's :5000 is up before HAL's :5001. No status LED state is used. Booting blue-breathing still shows during init. See [setup-flow.md](setup-flow.md#ap-mode).

## Ambient Idle Behaviors

When Lamp is idle (no interaction):
- **Breathing LED** — sine-wave brightness. Breathes the current LED color; when none is set (e.g. just after boot), it falls back to a soft warm white `(255, 200, 140)` (~2700K) at speed 0.3, so a lamp at rest reads as a cozy lamp turned on rather than a cold "device" blue. A user/agent-set color is respected (breathing uses it; ambient never overrides a locked color).

Auto-pauses on interaction, resumes after 60s of silence.

## LED in Emotion

See [emotion-led-mapping.md](emotion-led-mapping.md) for the full emotion → LED color + effect + servo mapping.

### Unknown emotion names

`POST /emotion` (`os/hal/routes/emotion.py`) never rejects a non-empty emotion name. Names are lowercased/trimmed; anything not in `EMOTION_PRESETS` falls back to `curious` (a neutral, always-safe expression) with a warning logged — callers are AI agents that sometimes invent emotion names, and a 400 would waste their turn with nothing showing on the device. Exception: while the device is sleeping, an unknown name is **ignored** (`status: ignored`) instead of falling back — `curious` is a wake emotion, so the fallback would let an invented name bypass the sleep gate and wake the device. Otherwise everything downstream (servo, LED) uses the resolved emotion.

## Per-device preset overrides

A device can override these emotion/scene/aim values (and the LED ring size) without
changing the shared defaults, via a `devices/<type>/presets.json` file. This is a
platform mechanism — see [DEVICE-SPEC.md § Per-device presets](../../../contract/DEVICE-SPEC.md#per-device-presets-presetsjson).
