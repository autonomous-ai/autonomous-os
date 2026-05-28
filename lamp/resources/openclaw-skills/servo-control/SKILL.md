---
name: servo-control
description: Use to aim/point/look the lamp in a DIRECTION, toggle servo state (hold/resume/release), or play a named servo animation (nod/shake/etc). Directions are fixed named locations or axes — supported: desk, wall, left, right, up, down, center, user. Furniture and surfaces ("desk", "table", "floor", "ceiling", "wall", "door", "workspace") are ALWAYS directions, never tracking targets — map them to the closest of the supported names (table/workspace → desk). MUST use /servo/aim (not /servo/track) for: "look at the desk"→desk, "point at my table"→desk, "look at the wall"→wall, "look left"→left, "point up"→up, "look at me"→user. For following a movable OBJECT by vision (cup, phone, hand, person, pet) use servo-tracking instead. Compound: if user names a direction AND an object ("look at desk and follow cup"), fire THIS aim skill first, then tracking.
---

# Servo Control

## Quick Start
Controls the lamp's 5-axis servo motors for aiming light direction and playing physical animations. Use `/servo/aim` for directional pointing, `/servo/play` for expressive animations.

## Workflow
1. Determine if the user wants to **aim** the light or **play an animation**.
2. Prefix reply with the appropriate `[HW:...]` marker — Lamp fires it before TTS.
3. Confirm the action to the user.

**Important**: For conversation reactions, use the **Emotion** skill instead — it combines servo + LED + eyes automatically.

## Examples

**Input:** "Point the light at my desk"
**Output:** `[HW:/servo/aim:{"direction":"desk"}]` Done, aimed the light at your desk.

**Input:** "Look at the desk" / "Look at my table" / "Look at my workspace"
**Output:** `[HW:/servo/aim:{"direction":"desk"}]` Looking at the desk now.

**Input:** "Look at the wall"
**Output:** `[HW:/servo/aim:{"direction":"wall"}]` Looking at the wall.

**Input:** "Look at me"
**Output:** `[HW:/servo/aim:{"direction":"user"}]` Looking at you!

**Input:** "Look to the left"
**Output:** `[HW:/servo/aim:{"direction":"left"}]` Looking left now.

**Input:** "Aim at the wall slowly"
**Output:** `[HW:/servo/aim:{"direction":"wall","duration":3.0}]` Aiming at the wall slowly.

**Input:** "Nod for me"
**Output:** `[HW:/servo/play:{"recording":"nod"}]` Nodding!

**Input:** "Turn left 15 degrees"
**Output:** `[HW:/servo/nudge:{"yaw":-15}]` Turned a bit to the left.

**Input:** "Tilt up a little"
**Output:** `[HW:/servo/nudge:{"pitch":10}]` Tilted up slightly.

**Input:** "Release the motors"
**Output:** `[HW:/servo/release:{}]` Servos released — you can move the lamp by hand now.

**Input:** "Stop moving" / "Hold still" / "Freeze" / "Stand still"
**Output:** `[HW:/servo/hold:{}]` OK, holding still.

**Input:** "Resume" / "Move again" / "You can move now" / "Start moving"
**Output:** `[HW:/servo/resume:{}]` Alright, back to normal!

## Tools

## How to Control Servo

**No exec/curl needed.** Inline markers at start of reply:

```
[HW:/servo/aim:{"direction":"desk"}] Aimed at your desk.
[HW:/servo/aim:{"direction":"left","duration":3.0}] Aiming left slowly.
[HW:/servo/play:{"recording":"nod"}] Nodding!
[HW:/servo/hold:{}] OK, holding still.
[HW:/servo/resume:{}] Back to normal!
[HW:/servo/release:{}] Servos released.
```

`duration` on `/servo/aim` controls move speed in seconds (default 2.0, 0 = instant).

### Nudge by degrees (relative movement)

```
[HW:/servo/nudge:{"yaw":-15}] Turned left 15 degrees.
[HW:/servo/nudge:{"pitch":10}] Tilted up a bit.
[HW:/servo/nudge:{"yaw":30,"pitch":-5,"duration":1.5}] Moved right and down.
```

| Parameter | Range | Description |
|---|---|---|
| `yaw` | -180 to 180 | Negative = left, positive = right |
| `pitch` | -90 to 90 | Negative = down, positive = up |
| `duration` | 0 to 10 | Move speed in seconds (default 2.0) |

### Available directions

| Direction | What it does |
|---|---|
| `center` | Neutral position, straight ahead |
| `desk` | Tilts down toward the desk surface |
| `wall` | Tilts up toward the wall behind |
| `left` | Turns left |
| `right` | Turns right |
| `up` | Points upward |
| `down` | Points downward |
| `user` | Slightly toward the user (default interaction pose) |

### Play animation

Available animations:

| Animation | When to use |
|---|---|
| `curious` | Something interesting, questions |
| `nod` | Agreement, acknowledgment |
| `headshake` | Disagreement, saying no |
| `happy_wiggle` | Joy, good news |
| `idle` | Resting state |
| `sad` | Empathy, bad news |
| `excited` | High energy, celebrations |
| `shy` | Bashful moments |
| `shock` | Surprise |
| `scanning` | Looking around, searching |
| `wake_up` | Waking up, starting a new session |
| `music_groove` | Grooving to music (auto-triggered during playback) |
| `music_chill` | Chill/lo-fi vibe (auto-triggered during calm music) |
| `music_hype` | High-energy hype (auto-triggered during EDM/party music) |
| `listening` | Attentive lean forward, user is speaking |
| `thinking_deep` | Slow deliberate look side-to-side, processing |
| `laugh` | Quick body shake, something funny |
| `confused` | Dog-like head tilt, did not understand |
| `sleepy` | Slow droop with catches, winding down |
| `greeting` | Wave gesture, saying hello |
| `goodbye` | Farewell wave, seeing someone off |
| `acknowledge` | Quick micro-nod (1.5s), confirming |
| `stretching` | Big extension + settle, after waking up |

### Hold position (stop moving)

```
[HW:/servo/hold:{}] OK, holding still.
```

Suppresses idle and ambient animations — lamp freezes in current pose. Emotions still play through (the lamp reacts when you talk, then holds still again). Call `/servo/resume` to return to normal.

**Triggers:** "stop moving", "hold still", "freeze", "don't move", "stand still"

### Resume from hold

```
[HW:/servo/resume:{}] Back to normal!
```

Exits hold mode and resumes idle animations.

**Triggers:** "resume", "move again", "you can move now", "start moving"

### Release servos (disable motors)

```
[HW:/servo/release:{}] Servos released.
```

Disables all servo motors so they can be moved freely by hand.

## Error Handling
- If the API returns an error or is unreachable, inform the user that servo control is temporarily unavailable.
- If an invalid direction is given, fall back to the closest matching direction from the available set.
- If an unknown animation is requested, list the available animations for the user.

## Rules
- **For conversation reactions, use the Emotion skill** — it calls servo automatically. Do not use this skill for emotional responses.
- Animations play once and return to rest position.
- Aim positions are persistent until changed.
- Use `/servo/aim` as the primary way to control light direction — do not use raw joint control unless testing.
- Always confirm the action to the user after execution.
- **Hold vs Release**: Hold keeps torque ON (lamp stays rigid in place). Release turns torque OFF (lamp goes limp). Use hold for "stop moving", release for "let me reposition the lamp by hand".
- **Hold is soft** — emotions still animate through, then the lamp holds still again. This keeps the lamp feeling alive during conversation while respecting the user's request to stop fidgeting.

## Output Template

```
[Servo] {action} — {direction_or_animation}
Status: {success|failed}
```
