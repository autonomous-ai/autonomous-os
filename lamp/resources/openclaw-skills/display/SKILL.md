---
name: display
description: Use when the user asks to change eye expression directly, show info text on the display (time, weather, timer), or manually control the round LCD — NOT needed for normal conversation (Emotion skill auto-syncs eyes).
---

# Display — Eyes & Info

## Quick Start
Controls the lamp's 1.28" round LCD display (GC9A01, 240x240). Two modes: animated eyes (default) and info text. Usually auto-synced by the Emotion skill — only call directly for manual eye control or info display.

## Workflow
1. Determine the mode needed:
   - **Eyes**: `[HW:/display/eyes:{"expression":"happy","pupil_x":0.0,"pupil_y":0.0}]`
   - **Info**: `[HW:/display/info:{"text":"14:30","subtitle":"Good afternoon"}]` then `[HW:/display/eyes-mode:{}]`
2. Place markers at start of reply — Lamp fires them before TTS. Skip silently if display unavailable.

**Important**: The Emotion skill auto-syncs eyes during conversation. Do not call both Emotion and Display for the same reaction.

## Examples

**Input:** "Look to the left"
**Output:** `[HW:/display/eyes:{"expression":"neutral","pupil_x":-1.0,"pupil_y":0.0}]` (no confirmation needed)

**Input:** "What time is it?"
**Output:** `[HW:/display/info:{"text":"14:30","subtitle":"Good afternoon"}]` It's 2:30 PM.

**Input:** "Show me a happy face"
**Output:** `[HW:/display/eyes:{"expression":"happy"}]` Here's a happy face!

## How to Control Display

**No exec/curl needed.** Inline markers at start of reply:

```
[HW:/display/eyes:{"expression":"happy","pupil_x":0.0,"pupil_y":0.0}]
[HW:/display/info:{"text":"14:30","subtitle":"Good afternoon"}]
[HW:/display/eyes-mode:{}]
```

- `pupil_x`: -1.0 (look left) to 1.0 (look right)
- `pupil_y`: -1.0 (look up) to 1.0 (look down)
- `text`: max 20 characters, `subtitle`: optional max 40 characters

### Available expressions

| Expression | When to use |
|---|---|
| `neutral` | Default resting state |
| `happy` | Good news, jokes, greetings |
| `sad` | Bad news, empathy |
| `curious` | Questions, interest |
| `thinking` | Processing, considering |
| `excited` | Big news, enthusiasm |
| `shy` | Bashful, compliments received |
| `shock` | Surprises |
| `sleepy` | Late night, tired |
| `angry` | Frustration (use sparingly) |
| `love` | Affection |

## Error Handling
- If `GET /display` returns unavailable or the API is unreachable, skip all display calls silently. Do not error out to the user.
- If an invalid expression is given, fall back to `neutral`.
- If text exceeds character limits, truncate gracefully.

## Rules
- **Emotion skill auto-syncs eyes** — when you call `POST /emotion`, the display updates automatically. Do not call both.
- **Info mode is temporary** — show info briefly, then switch back to eyes.
- The display is plugin hardware — it may not be available. Always check first, and skip silently if absent.
- Do not use this skill for normal conversation reactions — use the Emotion skill instead.

## Output Template

```
[Display] Mode: {eyes|info}
Expression: {expression} | Text: {text}
Status: {success|skipped|unavailable}
```
