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
| POST | `/led/paint` | Set per-pixel colors (array up to 64 items), or a gradient with `"gradient": true` |
| POST | `/led/off` | Turn off all LEDs |
| POST | `/led/effect` | Start an effect |
| POST | `/led/effect/stop` | Stop running effect |
| POST | `/led/restore` | Repaint user's saved LED state (or clear if none) |

### Transient writes

`/led/solid`, `/led/paint`, `/led/effect`, and `/led/off` accept an optional `"transient": true` flag. When set, the call paints the strip but does **not** overwrite the saved user LED state. The saved state is restored when the caller (e.g. Claude Desktop Buddy) is done — either via the natural emotion restore timer, or by an explicit `POST /led/restore`. Pulse effects launched with `transient: true` also overlay on the user's saved color instead of black.

## Solid Color

```json
POST /led/solid
{"color": [255, 180, 100]}
```

`color` is an `[R, G, B]` array (values 0-255) or a packed `0xRRGGBB` int.

## Paint (Per-Pixel / Gradient)

```json
POST /led/paint
{"colors": [[255, 0, 0], [0, 255, 0], [0, 0, 255]]}
```

`colors` is an array of `[R, G, B]` (or packed-int) pixels applied in index order (0-63). Without `gradient`, only the first `len(colors)` pixels are painted — the rest of the strip keeps its previous color.

```json
POST /led/paint
{"colors": [[0, 200, 200], [150, 0, 255]], "gradient": true}
```

With `"gradient": true` the colors are treated as gradient **stops** and linearly interpolated across the whole strip (CSS-gradient style) — the example above fades cyan → purple over all 64 pixels. Works with any number of stops ≥ 1.

Paint stops any running effect first (an effect repaints the strip every ~40ms and would overwrite it) and, unless `"transient": true`, saves the painted pixel list as the user LED state — so emotion animations, TTS waves, and HAL restarts within the same boot restore the exact gradient. For gradients the *expanded* 64-pixel list is saved, not the stops.

## Effects

```json
POST /led/effect
{"effect": "breathing", "color": [255, 100, 50], "speed": 1.0}
```

| Effect | Description | Params |
|--------|-------------|--------|
| `breathing` | Sine-wave brightness up/down | color, speed |
| `candle` | Random flickering candle | color |
| `rainbow` | Hue rotation across strip | speed |
| `notification_flash` | Quick flash 3 times | color |
| `pulse` | Single pulse from center outward | color, speed |

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

Managed by `system/statusled/Service` (lamp) and `lib/hal` directly (bootstrap).

None of these colors are hardcoded in Go anymore — `system/statusled` states, the
bootstrap OTA-progress colors, and the setup-needed white all flow through HAL. The OS
owns the state machine (WHEN a state shows) and sends the state *name* to HAL
(`POST /led/status`: booting/error/ota/connectivity/hal_down/agent_down/hardware/
ready_flash/ota_progress/ota_error/ota_success/setup); HAL resolves the color/effect/speed
from `STATUS_LED_PRESETS`, overridable per device via `presets.json`'s `status_led` section
(see [DEVICE-SPEC.md § Per-device presets](../../../devices/contract/DEVICE-SPEC.md#per-device-presets-presetsjson)).
`setup` is a persistent solid (saved as the displayed state); the rest are transient overlays.

### Mic-muted idle indicator

`STATUS_LED_PRESETS["mic_muted"]` — dark red `(10, 0, 0)` breathing at speed 0.8, far dimmer
than the `light.max_brightness` ceiling would force (the gate alone clamps to 120): it is a
resting look that stays lit for as long as the mic is muted, often pointed at the user, so it
is tuned to be glanceable rather than bright. Red helps — at the same value it carries about
a quarter the luminance of white. HAL-local
key (no Go statusled state): applied by `POST /voice/mute`, cleared by `POST /voice/unmute`
(`app_state._mic_muted_led`). It is the strip's **resting look** while the mic is muted —
nothing is blocked:

- Emotions, effects, TTS/music waves, and transient overlays all run normally on top.
  When they finish, every LED restore (`_restore_user_led`, `POST /led/restore`) settles
  back on the red instead of the user state — "nothing happening + red breathing" means
  the mic is muted.
- An explicit user LED command (non-transient `/led/solid|off|effect`, `/led/paint`)
  dismisses the indicator — the user's ask wins the strip; the mic stays muted.
- Yields to an active scene, which keeps its functional lighting (the flag persists, so
  leaving the scene while still muted brings the red back on the next restore). Scene
  mic-unmute paths (`/scene` with `mic:"on"`, `/scene/off`) also clear it. It does NOT
  yield to a resting (dark) strip: the indicator is the only signal that the mic is off,
  so it outranks "the lamp is idle".
- **Sleep wins:** while the `sleepy` emotion is active, the strip stays off. The muted
  flag still persists, but a late emotion/TTS/music restore cannot repaint the red
  indicator; it may resume only after a wake emotion clears sleep.
- `_user_led_state` is never touched — unmute restores the user's saved look.
- While the indicator owns the strip, transient overlay writes are skipped (`POST /led/effect`
  with `transient:true`) and so is **every** `POST /led/effect/stop`: no transient overlay can
  be running (its start was skipped), so any stop arriving while muted is a stale caller.
  Ambient's Go breathingLoop tracks its "running" flag locally and still fires StopEffect on
  pause/lock even though its start was skipped — before this guard covered all threads, that
  stop passed while an emotion effect held the strip (e.g. thinking's purple pulse) and killed
  it after ~one cycle, freezing the strip on the last ripple frame. Emotion effects settle
  back onto the red via their scheduled restore.

### Setup-needed solid (lamp)

When lamp starts and `config.SetUpCompleted == false` (device in AP/provisioning mode), `server/server.go` spawns a background goroutine that polls HAL `GET /health` once per second up to 30s, and once `health.led == true` fires `lelamp.SetSolid(255, 255, 255)` — paints the strip solid white as a "device ready, connect to my hotspot" cue. Polling (not a single call) handles the cold-boot race where os-server's :5000 is up before HAL's :5001. No status LED state is used. Booting blue-breathing still shows during init. See [setup-flow.md](../../../docs/setup-flow.md#ap-mode).

## Ambient Idle Behaviors

When Lamp is idle (no interaction):
- **Breathing LED** — sine-wave brightness. Breathes the current LED color; when none is set (e.g. just after boot), it falls back to the **resting look**, which is `(0, 0, 0)` — dark. A user/agent-set color is respected (breathing uses it; ambient never overrides a locked color).

Auto-pauses on interaction, resumes after 60s of silence.

### The resting look (default: off)

When no user LED state exists, the strip settles on the *resting look*, defined in **two
places that must be flipped together**:

| Side | Knob | Consumers |
|---|---|---|
| HAL | `AMBIENT_RESTING_LED` (`hal/presets.py`) | `POST /led/restore` with no user state; the settle after mic-unmute |
| os-server | `ambientRestingColor` (`system/ambient/service.go`) | `breathingLoop` fallback when `/led/color` reads black |

Both are currently **`(0, 0, 0)` — the resting state is dark**. A black resting color is
treated specially: the settle paths *clear* the strip instead of starting an effect (an
effect thread breathing black would burn 25 fps of SPI writes and make `GET /led/color`
report `on: true` over a dark strip), and the Go loop skips its tick entirely rather than
painting. Light is therefore opt-in — it comes on for an *action* (emotion, status cue,
explicit user/agent color, scene) and goes back to black when that action releases the
strip.

Two consequences worth knowing:

- An idle device looks **off**, not "resting". That is intended — status cues (`booting`,
  `connectivity`, …) are what tell the user something is happening.
- After a reboot the strip stays dark until something asks for light: the LED sidecar is
  boot-scoped, so every boot starts with no user state and lands on the resting look.

Setting both knobs back to `(255, 200, 140)` (warm white ~2700K @ speed 0.3) restores the
previous behavior, where an idle lamp read as a cozy lamp turned on rather than a cold
"device booting" blue, and the warm tone stayed clear of every status color. That look is
what re-lit the strip ~60s after the user turned the light off, and what made every fresh
boot come up lit.

### "Off" is not a mode

`POST /led/off` **clears the user LED state** (`_save_user_led_state(None)`) rather than
saving an off flag. Since the resting look is already dark, no state IS off. There are only
two states:

| State | `_user_led_state` | At rest | On an action |
|---|---|---|---|
| **Default** | `None` | dark | lights up (emotion, status cue, mic-muted indicator) |
| **User colour** | solid / paint / effect / scene | that colour | effect runs over it, then settles back |

`led_should_stay_dark()` (`hal/app_state.py`) is the single predicate for "leave the lamp
alone", and everything that paints without the user asking checks it: the TTS/music waves,
the post-effect settle, `POST /led/restore`, `POST /led/effect/stop` (it clears the stopped
effect's last frame instead of leaving it frozen), presence restore/dim, and — on the
os-server side — ambient's breathing loop.

What is deliberately NOT gated: an explicit user/agent command (that IS the user asking, and
it overwrites the state), and cues that carry information the user needs — status overlays
(`POST /led/status`: connectivity orange, error red, OTA green) and the mic-muted indicator.
Those earn their light even on a resting strip.

Off used to be its own sticky state, and it was worse: it looked identical to the default
(both dark) but behaved differently, nothing could return the device to the default — an
explicit colour was the only way out — and a reboot silently dropped it, because the sidecar
is boot-scoped. A legacy sidecar holding `{"type": "off"}` is normalised to "no state" on
load.

## LED in Emotion

See [emotion-led-mapping.md](emotion-led-mapping.md) for the full emotion → LED color + effect + servo mapping.

### Unknown emotion names

`POST /emotion` (`hal/routes/emotion.py`) never rejects a non-empty emotion name. Names are lowercased/trimmed; anything not in `EMOTION_PRESETS` falls back to `curious` (a neutral, always-safe expression) with a warning logged — callers are AI agents that sometimes invent emotion names, and a 400 would waste their turn with nothing showing on the device. Exception: while the device is sleeping, an unknown name is **ignored** (`status: ignored`) instead of falling back — `curious` is a wake emotion, so the fallback would let an invented name bypass the sleep gate and wake the device. Otherwise everything downstream (servo, LED) uses the resolved emotion.

## Per-device preset overrides

A device can override these emotion/scene/aim values (and the LED ring size) without
changing the shared defaults, via a `devices/<type>/presets.json` file. This is a
platform mechanism — see [DEVICE-SPEC.md § Per-device presets](../../../devices/contract/DEVICE-SPEC.md#per-device-presets-presetsjson).
