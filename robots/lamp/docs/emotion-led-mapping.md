# Emotion → LED + Animation Mapping

Source: colors are what the **lamp** actually shows — `robots/lamp/presets.json` (the per-device overlay) merged over `hal/presets.py` `EMOTION_PRESETS`. The overlay only replaces `color`; effect, speed and servo always come from the base presets. The `Color source` column says whether a row's color comes from the lamp `overlay` or is still the untouched `base` value.

| Emotion | Color (RGB) | Hex | Color source | Effect | Speed | Servo Animation |
|---|---|---|---|---|---|---|
| `curious` | 0, 12, 0 | `#000c00` green | overlay | candle | 0.3 | curious |
| `happy` | 12, 12, 0 | `#0c0c00` yellow | overlay | candle | 0.2 | happy_wiggle |
| `sad` | 16, 0, 0 | `#100000` red | overlay | breathing | 0.4 | sad |
| `thinking` | 0, 12, 0 | `#000c00` green | overlay | pulse | 0.3 | — (see note) |
| `idle` | 8, 4, 0 | `#080400` dim amber | overlay | breathing | 0.2 | idle |
| `excited` | 12, 12, 0 | `#0c0c00` yellow | overlay | candle | 0.5 | excited |
| `shy` | 16, 0, 0 | `#100000` red | overlay | breathing | 0.3 | shy |
| `shock` | 12, 12, 12 | `#0c0c0c` soft white | base | notification_flash | 1.0 | shock |
| `listening` | 0, 0, 16 | `#000010` blue | overlay | breathing | 1.2 | — (see note) |
| `laugh` | 12, 12, 0 | `#0c0c00` yellow | overlay | candle | 0.2 | laugh |
| `confused` | 16, 0, 0 | `#100000` red | overlay | candle | 0.2 | confused |
| `sleepy` | 0, 0, 0 | `#000000` black (off) | base | solid | — | sleepy |
| `greeting` | 16, 0, 16 | `#100010` purple | overlay | breathing | 0.3 | greeting \| wake_up |
| `goodbye` | 16, 0, 16 | `#100010` purple | overlay | breathing | 0.5 | goodbye |
| `caring` | 16, 0, 16 | `#100010` purple | overlay | breathing | 0.4 | nod |
| `acknowledge` | 0, 12, 0 | `#000c00` green | overlay | breathing | 0.5 | acknowledge |
| `stretching` | 8, 4, 0 | `#080400` dim amber | overlay | breathing | 0.6 | stretching |
| `music_strong` | 8, 12, 8 | `#080c08` pale green (no effect — see below) | base | rainbow | 1.0 | music_rock |
| `music_chill` | 0, 12, 12 | `#000c0c` cyan | overlay | breathing | 0.3 | music_rock \| music_groove \| music_jazz \| music_waltz |
| `scan` | 0, 12, 0 | `#000c00` green | overlay | pulse | 0.3 | scanning |
| `nod` | 8, 4, 0 | `#080400` dim amber | overlay | breathing | 0.5 | nod |
| `headshake` | 16, 0, 0 | `#100000` red | overlay | breathing | 0.5 | headshake |

`music_strong`'s color is inert: it runs the `rainbow` effect, and `rainbow()` in `hal/drivers/rgb/effects.py` ignores the `color` argument and sweeps the whole hue circle itself — which is why the overlay does not bother to set it.

## Six hue groups

The table above is the base palette from `hal/presets.py`. On the lamp it is overridden, because at indicator brightness that palette has no usable color space left.

Measured on lamp-0c89 (19/08/2026): 22 emotions are packed into three hue clusters, and **12 of them sit between hue 20° and 44°** — caring 20, headshake 20, shy 21, greeting 26, goodbye 26, confused 28, music_chill 34, idle 38, laugh 38, nod 38, curious 40, happy 44. Some are not merely close but byte-identical: `idle` = `laugh` = `nod` = `[12, 8, 1]`, and `greeting` = `goodbye` = `[12, 8, 5]`. At a peak of 12–16 each channel has only 12–16 levels instead of 255, so a 1–4° hue difference is simply not resolvable by eye.

This is fallout from the dimming pass. Before 18/08 the presets ran at high peaks — greeting/goodbye `[255, 180, 100]`, caring `[255, 160, 120]`, music_chill `[252, 136, 3]`, acknowledge `[51, 230, 70]`, listening `[51, 121, 230]` — and at that amplitude 22 distinct colors read fine. Users complained about glare ("like a camera flash in your face": greeting ran full 255 across all 64 pixels exactly as someone walked up to the lamp), so everything was pulled down to 12–16. That cured the glare and collapsed the color space.

**Turning the brightness back up is not the fix.** With gamma 2.2, dropping peak from 255 to 90 costs only ~40% of *perceived* brightness while keeping 90 color levels; dropping 90 → 12 costs another ~40% of perceived brightness but throws away 7.5× the color resolution. Almost all of the anti-glare benefit is already won in the first step; the second step is nearly pure cost.

So the lamp keeps the peak exactly where it is (12/16 — **no increase in total light at all**) and spends the remaining headroom on hue instead: six groups, 60° apart.

| Group | Hue | RGB | Emotions |
|---|---|---|---|
| negative | 0° red | `[16, 0, 0]` | `sad`, `shy`, `confused`, `headshake` |
| joy | 60° yellow | `[12, 12, 0]` | `happy`, `laugh`, `excited` |
| processing | 120° green | `[0, 12, 0]` | `curious`, `thinking`, `scan`, `acknowledge` |
| music | 180° cyan | `[0, 12, 12]` | `music_chill` |
| listening | 240° blue | `[0, 0, 16]` | `listening` |
| social | 300° purple | `[16, 0, 16]` | `greeting`, `goodbye`, `caring` |
| background | 30° amber, peak 8 | `[8, 4, 0]` | `idle`, `nod`, `stretching` |
| alarm | white | `[12, 12, 12]` | `shock` (unchanged) |
| sleep | off | `[0, 0, 0]` | `sleepy` (unchanged) |

Three things are deliberate:

1. **Every color has at least one channel at 0** — maximum saturation. At a peak of 12–16 this is mandatory: a diluted color like `[12, 8, 1]` loses whatever made it itself, while `[0, 12, 0]` still reads unmistakably green no matter how faint it gets.
2. **`idle` / `nod` / `stretching` drop to peak 8**, one step below every other emotion. `idle` is the state the lamp spends the most time in, so it deserves to recede — and this *lowers* total light output rather than raising it. Confirmed on the real lamp by the user as less glaring.
3. **Within a group, emotions are told apart by effect + speed, not by color** — e.g. the joy group: `happy` candle 0.2, `laugh` candle 0.2, `excited` candle 0.5. The eye discriminates rhythm far better than it discriminates 4° of hue.

The trade-off, stated plainly: 22 emotions now share 6 colors. From the color alone you can read the **group**, not the specific emotion. That is accepted because the situation it replaced let you read nothing at all.

Two technical notes:

- **`music_strong` is intentionally absent** from the override table. It uses the `rainbow` effect, and `rainbow()` in `hal/drivers/rgb/effects.py` ignores the `color` argument entirely — it sweeps the whole hue circle itself. Assigning it a color would mean nothing.
- **This table lives in `robots/lamp/presets.json`**, the per-device overlay merged field by field at boot via `hal/board/presets_overlay.py` — `hal/presets.py` is *not* edited. Other robots (reachy, intern) therefore keep the base palette, and reverting the lamp to the base palette is just deleting the `emotion` section from that JSON file.
- **Do not confuse `EMO_IDLE` with `AMBIENT_RESTING_LED`.** The latter is `[0, 0, 0]` (product call 30/07/2026: a resting strip is fully off); `EMO_IDLE` is an emotion the agent actively emits and still has a color.

## `thinking` and `listening` have no servo

Both set `"servo": None` — LED only, the lamp holds still:

- `listening` runs while the user is actually speaking; servo noise plus chassis vibration goes straight into the mic and dirties STT.
- `thinking` is fired by the emotion-ack hook on **every** preprocessed message, so a servo there means the body fidgets through the whole conversation. It also moves the camera: `thinking_deep.csv` sweeps wrist_pitch 34° and wrist_roll 32°, and the camera sits in the head — so when the realtime `look` tool fires (it fires during the model's turn, i.e. while `thinking` is showing) the user has to chase a moving, rolling camera to hold an object in frame. The same hook is why its LED sits behind the `_BACKGROUND_EMOTIONS` guard in `hal/app_state.py`, to keep a whole conversation from being repainted green.

On the lamp `thinking` gets `"servo": null` from `robots/lamp/presets.json`; the base preset in `hal/presets.py` still maps it to `thinking_deep` for bodies that want it.

`listening.csv` and `thinking_deep.csv` stay in `hal/recordings/` even with no emotion mapping to them: `/servo/play` can still call them by hand, and Reachy still maps them (`hal/drivers/motors/reachy_service.py`).

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

`candle()` (`hal/drivers/rgb/effects.py`) gives each pixel its own flicker level in `[CANDLE_FLICKER_MIN, 1.0]` (`CANDLE_FLICKER_MIN = 0.8`) and scales **all three channels by that one level**: `tuple(min(255, int(c * level)) for c in color)`. Every pixel is the same color, only at a different brightness — the strip reads as one flame breathing unevenly instead of a handful of different colors.

This is the same rule as the presets themselves: dim by scaling, never by picking a new color. The hue is what the agent is trying to say.

It used to break that rule. The old implementation treated the channels separately — `r = color[0]*flicker + random.randint(0, 20)`, `g = color[1]*flicker*random.uniform(0.6, 0.9)`, `b = color[2]*flicker*0.3` — which was survivable when emotion colors were bright, but not after they were dimmed to indicator levels where peaks are only 12–16. A `+20` added to red is then **larger than the color itself**. Measured on the lamp (19/08/2026): `happy`, declared `[12, 9, 1]` at hue 44° yellow, painted pixels ranging from `(9, 5, 0)` to `(26, 4, 0)` — red up to 2.2× its own declared value, hue collapsed to 5–20°, so the eye saw a scatter of oranges instead of an even yellow. `excited` `[12, 8, 12]` (pink-purple, hue 300°) was worst: blue crushed ×0.3 while red was inflated, and it came out orange. After the fix, measured hue held: happy 36–48°, curious 36–43°, excited exactly 300°.

Emotions affected: `curious`, `happy`, `excited`, `laugh`, `confused` — the five that use candle. `breathing` and `pulse` never had this bug because they only ever scale proportionally (`int(c * brightness)`).

### …and it no longer strobes

The same pass fixed a second, independent problem in `candle`: it used to pick a fresh random level per pixel **every frame**, at `0.05/speed` — 4 Hz for `happy`/`laugh`/`confused`, 10 Hz for `excited` — stepping anywhere between 0.4 and 1.0 of the base color. That is a 60% modulation depth at 4–10 Hz, and IEEE 1789-2015 places **any** flicker below 90 Hz in its high-risk band (at 100 Hz the limit is already 1.6%). The 3–70 Hz range is the one linked to headaches and visual discomfort, and on a desk lamp pointed at a face it was reported as dizzying.

Two changes bring it inside the guidance without changing what the effect is:

- **Fixed 30 Hz refresh with interpolation** (`CANDLE_REFRESH_HZ = 30`). Each pixel now travels toward its target level instead of jumping to it — `speed` sets how fast it travels rather than how often it teleports. Steps are what the eye locks onto; a smooth ramp of the same period reads as motion, not flicker.
- **Floor raised 0.4 → 0.8**, cutting modulation depth from 60% to 20%.

Measured on the lamp afterwards: `happy` 29.2 Hz refresh at 9.1% modulation, `excited` 29.1 Hz at 18.2%, and the perceived flicker rate lands between 0.85 Hz (speed 0.2) and 2.25 Hz (`excited`, speed 0.5) — under the 3 Hz start of the uncomfortable band. The `speed * 0.6` factor in the approach term is what keeps `excited` there; raising it puts the liveliest preset back into the band.

`CANDLE_FLICKER_MIN` and `CANDLE_REFRESH_HZ` are comfort bounds, not taste settings. Do not lower either without re-reading the standard.

## LED Restore Behavior

- **User has set a color/effect/scene** → after the emotion, restore the user's color/scene (with a re-aim if it is a scene)
- **Light is off or never set** → the emotion LED stays after the animation ends
- **`shock`** → restore after 2.0s (notification_flash self-clears after ~1.5s)
- **`idle`** → no restore scheduled (it is the ambient resting state)

## Pulse Behavior

Emotion-driven pulse (thinking / listening / scan) runs on a **black base**: the purple/green wavefront stands out on a dark strip, so the agent's expression is visible no matter what color the user has set.

Transient pulse (Buddy busy, other driver overlays via `/led/effect` with `transient: true`) **overlays the user's color** instead: pixels outside the wavefront keep the user color, and wavefront pixels alpha-blend from user → emotion. The point is to keep the user's background color continuous underneath a quick overlay.

Source: `hal/drivers/rgb/effects.py:pulse()`; the emotion path is `hal/app_state.py:_apply_emotion_led_display()` (black base by default), the transient path is `hal/routes/led.py:start_led_effect()` (base = `_get_user_base_color()` when `transient=true`).
