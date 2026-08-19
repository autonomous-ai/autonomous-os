# Emotion → LED + Animation Mapping

Source: `hal/presets.py` — `EMOTION_PRESETS`

| Emotion | Color (RGB) | Hex | Effect | Speed | Servo Animation |
|---|---|---|---|---|---|
| `curious` | 12, 8, 0 | `#0c0800` warm yellow | candle | 0.3 | curious |
| `happy` | 12, 9, 1 | `#0c0901` yellow | candle | 0.2 | happy_wiggle |
| `sad` | 16, 8, 8 | `#100808` deep red | breathing | 0.4 | sad |
| `thinking` | 6, 12, 4 | `#060c04` muted green | pulse | 0.3 | thinking_deep |
| `idle` | 12, 8, 1 | `#0c0801` dim yellow | breathing | 0.2 | idle |
| `excited` | 12, 8, 12 | `#0c080c` pink-purple | candle | 0.5 | excited |
| `shy` | 16, 7, 2 | `#100702` pink | breathing | 0.3 | shy |
| `shock` | 12, 12, 12 | `#0c0c0c` soft white | notification_flash | 1.0 | shock |
| `listening` | 4, 8, 16 | `#040810` blue | breathing | 1.2 | — (see note) |
| `laugh` | 12, 8, 1 | `#0c0801` deep yellow | candle | 0.2 | laugh |
| `confused` | 16, 9, 3 | `#100903` dark orange | candle | 0.2 | confused |
| `sleepy` | 0, 0, 0 | `#000000` black (off) | solid | — | sleepy |
| `greeting` | 12, 8, 5 | `#0c0805` pale yellow | breathing | 0.3 | greeting \| wake_up |
| `goodbye` | 12, 8, 5 | `#0c0805` pale yellow | breathing | 0.5 | goodbye |
| `caring` | 12, 8, 6 | `#0c0806` orange-pink | breathing | 0.4 | nod |
| `acknowledge` | 3, 12, 4 | `#030c04` green | breathing | 0.5 | acknowledge |
| `stretching` | 12, 12, 2 | `#0c0c02` pale green | breathing | 0.6 | stretching |
| `music_strong` | 8, 12, 8 | `#080c08` pale green | rainbow | 1.0 | music_rock |
| `music_chill` | 16, 9, 0 | `#100900` orange | breathing | 0.3 | music_rock \| music_groove \| music_jazz \| music_waltz |
| `scan` | 5, 12, 3 | `#050c03` light green | pulse | 0.3 | scanning |
| `nod` | 12, 8, 1 | `#0c0801` earth orange | breathing | 0.5 | nod |
| `headshake` | 16, 6, 1 | `#100601` amber | breathing | 0.5 | headshake |

## `listening` has no servo

It is the only preset that sets `"servo": None` — LED only, the lamp holds still. `listening` runs while the user is actually speaking; servo noise plus chassis vibration goes straight into the mic and dirties STT.

`thinking` does have a servo (`thinking_deep`), but it is still a special case on the LED side: the emotion-ack hook fires it on **every** preprocessed message, so its LED sits behind the `_BACKGROUND_EMOTIONS` guard in `hal/app_state.py` to keep a whole conversation from being repainted green.

`listening.csv` stays in `hal/recordings/` even though no emotion maps to it: `/servo/play` can still call it by hand, and Reachy still maps it (`hal/drivers/motors/reachy_service.py`).

The code path handles `servo: None` natively — `hal/routes/emotion.py` skips the play branch and `POST /emotion` returns `"servo": null`, and `listening` schedules no LED restore at all.

### `servo: None` alone does NOT hold the lamp still

Not starting a new recording does not mean the lamp is quiet. The idle loop has been running since boot and repeats forever (`_continue_playback` in `hal/drivers/motors/animation_service.py`), and `idle.csv` is not gentle — each 10s cycle sweeps wrist_roll ~32°, wrist_pitch ~26°, base_pitch ~17°. Worse, an emotion that just finished **interpolates back to idle** over a few seconds, so the widest swing lands exactly while the user is talking.

So for a `servo: None` emotion (today only `listening`) the route calls `svc.halt()`: drop the running recording, pin the current pose, keep torque ON. No explicit un-halt is needed — the next emotion or `/servo/play` calls `_begin_motion()`, which clears the flag itself.

Two guards go with it:

- **Music is exempt**: while music plays the groove matters more, and a listening cue must not stop the dancing.
- **Auto-resume idle after 10s** (`STILL_IDLE_RESUME_SECONDS` in `hal/routes/emotion.py`): if the turn produces no emotion at all (LLM error, silence after the first partial), the body returns to idle instead of freezing mid-pose. Any `POST /emotion` cancels this timer. The 8s safety net in `voice_service` only clears the LED and never touches the servo — so this timer is the only thing looking after the body.

Measured on lamp-0c89: after `listening`, all 5 servo angles held at T+2s / T+5s / T+8s, and idle resumed around T+13s; a `happy` sent mid-halt played normally.

## Brightness budget (peak)

Emotion LEDs are an **indicator**, not illumination — they share a budget with `STATUS_LED_PRESETS`:

- green-leaning hues (green / yellow / cyan / white) → peak channel **12**
- little or no green (red / purple / orange / blue) → peak channel **16**

Each color is brought down by **scaling the original RGB proportionally** to its tier, so every emotion keeps its original hue. Dimming must be done by scaling, not by picking a new color — the hue is what the agent is trying to say; brightness is only how loudly it says it.

The `light.max_brightness` gate (lamp: 120) only scales peak **UP** toward the ceiling, never down, so dimming has to happen in the preset itself. After any change, check it by eye on a real device.

`listening` uses breathing rather than pulse: it is lit while the user speaks, and pulse's dark gaps between beats read as a warning when sustained.

If `blink` is ever used: `blink()` maps speed 1.0 → **~3 Hz** (`hal/drivers/rgb/effects.py`), fast enough to hurt the eyes. Keep it ≤ 0.5 (~1.5 Hz or slower).

## `candle` varies brightness, never hue

`candle()` (`hal/drivers/rgb/effects.py`) gives each pixel its own flicker factor in `[CANDLE_FLICKER_MIN, 1.0]` (`CANDLE_FLICKER_MIN = 0.4`) and scales **all three channels by that one factor**: `tuple(min(255, int(c * flicker)) for c in color)`. Every pixel is the same color, only at a different brightness — the strip reads as one flame breathing unevenly instead of a handful of different colors.

This is the same rule as the presets themselves: dim by scaling, never by picking a new color. The hue is what the agent is trying to say.

It used to break that rule. The old implementation treated the channels separately — `r = color[0]*flicker + random.randint(0, 20)`, `g = color[1]*flicker*random.uniform(0.6, 0.9)`, `b = color[2]*flicker*0.3` — which was survivable when emotion colors were bright, but not after they were dimmed to indicator levels where peaks are only 12–16. A `+20` added to red is then **larger than the color itself**. Measured on the lamp (19/08/2026): `happy`, declared `[12, 9, 1]` at hue 44° yellow, painted pixels ranging from `(9, 5, 0)` to `(26, 4, 0)` — red up to 2.2× its own declared value, hue collapsed to 5–20°, so the eye saw a scatter of oranges instead of an even yellow. `excited` `[12, 8, 12]` (pink-purple, hue 300°) was worst: blue crushed ×0.3 while red was inflated, and it came out orange. After the fix, measured hue held: happy 36–48°, curious 36–43°, excited exactly 300°.

Emotions affected: `curious`, `happy`, `excited`, `laugh`, `confused` — the five that use candle. `breathing` and `pulse` never had this bug because they only ever scale proportionally (`int(c * brightness)`).

## LED Restore Behavior

- **User has set a color/effect/scene** → after the emotion, restore the user's color/scene (with a re-aim if it is a scene)
- **Light is off or never set** → the emotion LED stays after the animation ends
- **`shock`** → restore after 2.0s (notification_flash self-clears after ~1.5s)
- **`idle`** → no restore scheduled (it is the ambient resting state)

## Pulse Behavior

Emotion-driven pulse (thinking / listening / scan) runs on a **black base**: the purple/green wavefront stands out on a dark strip, so the agent's expression is visible no matter what color the user has set.

Transient pulse (Buddy busy, other driver overlays via `/led/effect` with `transient: true`) **overlays the user's color** instead: pixels outside the wavefront keep the user color, and wavefront pixels alpha-blend from user → emotion. The point is to keep the user's background color continuous underneath a quick overlay.

Source: `hal/drivers/rgb/effects.py:pulse()`; the emotion path is `hal/app_state.py:_apply_emotion_led_display()` (black base by default), the transient path is `hal/routes/led.py:start_led_effect()` (base = `_get_user_base_color()` when `transient=true`).
