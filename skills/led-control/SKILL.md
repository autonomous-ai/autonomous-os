---
name: led-control
description: Control the device's RGB light when the user asks for a SPECIFIC color (e.g. "yellow", "red", "yellow", "red", "turn on color X", "enable X light"), an LED effect, or turning LEDs off. Do NOT use for ambiance/activity lighting (use Scene) or emotion expression (use Emotion).
---

# LED Control

## Quick Start
Control the device's RGB light directly. Use this skill only when the user requests a specific color, effect, or to turn LEDs off.

## Workflow
1. Determine the user's intent:
   - Specific color -> `[HW:/led/solid:{"color":[R,G,B]}]`
   - Two or more colors / mix / gradient -> `[HW:/led/paint:{"colors":[[R,G,B],[R,G,B]],"gradient":true}]`
   - Effect -> `[HW:/led/effect:{"effect":"name","color":[R,G,B],"speed":1.0}]`
   - Turn off -> `[HW:/led/off:{}]`
2. Place markers at start of reply — the device fires them in order before TTS
3. Confirm the action to the user

## Examples

Input: "Make it purple" / "turn on purple"
Output: `[HW:/led/solid:{"color":[100,50,200]}]` I've set the LEDs to purple.

Input: "Turn on yellow light" / "set yellow" / "switch to yellow" / "yellow light"
Output: `[HW:/led/solid:{"color":[255,220,0]}]` Yellow light on!

Input: "Turn on red light" / "set red" / "switch to red" / "red light"
Output: `[HW:/led/solid:{"color":[255,0,0]}]` Red light on!

Input: "Turn on white light" / "set white" / "white light"
Output: `[HW:/led/solid:{"color":[255,255,255]}]` White light on!

Input: "Change to cyan purple" / "mix blue and pink" / "gradient from red to orange"
Output: `[HW:/led/paint:{"colors":[[0,200,200],[150,0,255]],"gradient":true}]` Cyan-to-purple gradient on!

Input: "Do a breathing light with warm color"
Output: `[HW:/led/effect:{"effect":"breathing","color":[255,180,100],"speed":0.5}]` Breathing effect started with a warm glow.

Input: "Rainbow mode!"
Output: `[HW:/led/effect:{"effect":"rainbow","speed":1.0}]` Rainbow effect is running!

Input: "Turn off the lights"
Output: `[HW:/led/off:{}]` LEDs are off.

Input: "I want to relax" / "reading mode" / "goodnight"
Output: Do NOT use this skill. Use **Scene** skill instead.

Input: Conversational reply needing emotion
Output: Do NOT use this skill. Use **Emotion** skill instead.

## How to Control LEDs

**No exec/curl needed.** Use inline markers at start of reply — the device fires them in order:

### Solid color (automatically stops the current effect)
```
[HW:/led/solid:{"color":[255,220,0]}] Yellow light on!
```
Color is an RGB array `[R, G, B]`.

### Multi-color / gradient (automatically stops the current effect)
```
[HW:/led/paint:{"colors":[[0,200,200],[150,0,255]],"gradient":true}] Cyan-to-purple gradient!
```
- `colors`: list of RGB gradient stops (2+ colors), blended smoothly across the whole strip
- `gradient: true` is REQUIRED — without it the colors paint only the first N pixels and the rest of the strip keeps its old color
- Works with 3+ stops too: `{"colors":[[255,0,0],[255,220,0],[0,200,80]],"gradient":true}` = red -> yellow -> green

### Effect
```
[HW:/led/effect:{"effect":"breathing","color":[255,180,100],"speed":0.5}] Breathing warm glow.
```
- `effect` (required): effect name from table below
- `color` (optional): RGB array
- `speed` (optional): 0.1 (slow) to 5.0 (fast), default 1.0
- `duration_ms` (optional): auto-stop after N ms

### Turn off / stop effect
```
[HW:/led/off:{}] LEDs off.
[HW:/led/effect/stop:{}]
```

### Available effects

| Effect | Description | Best for |
|---|---|---|
| `breathing` | Slow fade in/out with given color | Relaxation, idle ambient, meditation |
| `candle` | Warm flickering like a real candle | Cozy evening, romantic mood |
| `rainbow` | Hue cycle across all pixels | Fun, party, showing off |
| `notification_flash` | 3 quick flashes then auto-stops | Alerts, timer done, reminders |
| `pulse` | Radial brightness wave from center | Attention, heartbeat, alive feeling |

### Color suggestions

| Color name | Color (RGB) |
|---|---|
| White | `[255, 255, 255]` |
| Yellow | `[255, 220, 0]` |
| Warm white | `[255, 180, 100]` |
| Orange | `[255, 100, 0]` |
| Red | `[255, 0, 0]` |
| Green | `[0, 200, 80]` |
| Blue | `[0, 150, 255]` |
| Purple | `[100, 50, 200]` |
| Pink | `[255, 80, 150]` |

## Error Handling
- If the API returns an error or is unreachable, inform the user: "I couldn't control the LEDs right now. The hardware service may be unavailable."
- If the user requests an unknown effect name, pick the closest match from the available effects table or tell the user which effects are available.

## Rules
- **ALWAYS include the JSON body, even for no-argument commands.** Every marker is `[HW:/path:{...}]` — for commands that take no arguments the body is the empty object `{}`. Emit `[HW:/led/off:{}]` and `[HW:/led/effect/stop:{}]`, **NEVER** the bodyless `[HW:/led/off]` or `[HW:/led/effect/stop]`. A marker without `:{...}` is malformed and the device silently drops it — the light will NOT turn off.
- **The marker is NOT a markdown link.** Never wrap it as `[some text](HW:/led/off:{})` — emit the plain marker `[HW:/led/off:{}]` followed by your sentence as normal text.
- **"Turn on color X" / "set light X" / "change color X" = THIS skill.** Any request naming a color (yellow, red, green, purple, white, orange, pink…) routes here — NOT to Emotion or Scene. Emotion yellow/happy is for YOUR feelings, not user's lighting request.
- **NEVER use `/led-color` or `/led/color` for setting color — these endpoints do NOT exist.** Always use `[HW:/led/solid:{"color":[R,G,B]}]`.
- **Two or more colors at once = `/led/paint` with `"gradient":true`.** `/led/solid` takes exactly ONE color — never try to express "cyan purple" as a single blended RGB; paint a gradient instead.
- **Solid and paint stop effects automatically.** Their HAL endpoints stop the running effect before updating the strip; do not add a separate stop marker. Keep `/led/effect/stop` for an explicit stop-only request.
- **Solid colors = full brightness.** For dim/ambient lighting, use the Scene skill instead.
- **Effects run until stopped** (unless `duration_ms` is set). Starting a new effect auto-stops the previous one.
- `/led/off` also stops any running effect.
- For "make it cozy" or "candle light" -> use `candle` effect, NOT a static orange color.
- For "breathing" or "pulsing" requests -> use the matching effect.
- Combine effects with low speed (0.3-0.5) for calm moods, high speed (2.0-3.0) for energy.
- **Do NOT use for activity/ambiance lighting** (sleeping, relaxing, reading, focus, movie) -> use **Scene** skill.
- **Do NOT use for expressing emotion** -> use **Emotion** skill.

## Output Template
```
[HW:/led/solid:{"color":[100,50,200]}] Purple light on.
```
Examples:
- `[HW:/led/solid:{"color":[100,50,200]}] Purple light on.`
- `[HW:/led/effect:{"effect":"breathing","color":[255,180,100],"speed":0.5}] Warm breathing light on.`
- `[HW:/led/off:{}] Lights off.`
