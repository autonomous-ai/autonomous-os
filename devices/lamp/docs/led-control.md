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
- `greeting`, `sleepy`, `stretching` → **allowed** (these signal state changes: wake, sleep, scene transition)

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

### Setup-needed solid (lamp)

When lamp starts and `config.SetUpCompleted == false` (device in AP/provisioning mode), `server/server.go` spawns a background goroutine that polls HAL `GET /health` once per second up to 30s, and once `health.led == true` fires `lelamp.SetSolid(255, 255, 255)` — paints the strip solid white as a "device ready, connect to my hotspot" cue. Polling (not a single call) handles the cold-boot race where os-server's :5000 is up before HAL's :5001. No status LED state is used. Booting blue-breathing still shows during init. See [setup-flow.md](setup-flow.md#ap-mode).

## Ambient Idle Behaviors

When Lamp is idle (no interaction):
- **Breathing LED** — sine-wave brightness, warm palette

Auto-pauses on interaction, resumes after 60s of silence.

## LED in Emotion

See [emotion-led-mapping.md](emotion-led-mapping.md) for the full emotion → LED color + effect + servo mapping.

## Per-device preset overrides

The emotion / scene / aim preset *values* in `os/hal/presets.py` (colors, brightness,
servo positions) and the LED ring size are the **platform default** — every device
inherits them. A device overrides only the values it wants different by shipping a
sparse `devices/<type>/presets.json`. At HAL startup `board/presets_overlay.py`
deep-merges this file onto the base tables in place, before any route or driver
reads them; a device with no `presets.json` keeps the base verbatim (Lamp does).

```json
{
  "led_count": 60,
  "emotion": { "listening": { "color": [255, 120, 0] } },
  "scene":   { "relax":     { "brightness": 0.3 } },
  "aim":     { "desk":      { "base_pitch.pos": 8.0 } }
}
```

Rules:
- Every section (`led_count`, `emotion`, `scene`, `aim`) is optional.
- Each entry patches the matching base entry **field-by-field** — only the named
  fields change (e.g. override just `listening.color`, keep its effect/speed).
- An override naming a preset that does not exist in the base table (a typo) **fails
  loud at boot**, as does a malformed file or a non-positive `led_count`.
- HAL-only: presets are LED/servo look-and-feel; the OS core (Go) does not read them.
  An override only takes effect for routes the device actually mounts — e.g. an
  `emotion.*` override does nothing on a device without the `expression` capability
  (no `/emotion` route), and `scene`/`aim` need `light`/`motion` respectively.

Copy-paste reference: [`devices/_base/presets.example.json`](../../_base/presets.example.json)
(an annotated template covering every section — it is `.example.json`, so HAL never
loads it; rename to `presets.json` in your device folder to activate).
