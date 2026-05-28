---
name: scene
description: Activate predefined lighting scene presets (reading, focus, relax, movie, night, energize) when the user asks for activity-based or environment lighting. Scenes control both color temperature AND brightness. Do NOT use for specific colors (use LED Control) or emotion expression (use Emotion).
---

# Lighting Scenes

## Quick Start
Activate predefined lighting presets that set optimal color temperature and brightness for specific activities. Use this for ALL activity-based or environment lighting requests.

## Workflow
1. Determine which scene matches the user's request or activity
2. Prefix reply with `[HW:/scene:{"scene":"name"}]` — Lamp fires it before TTS
3. Confirm the scene activation to the user
4. Optionally chain emotion: `[HW:/scene:{"scene":"night"}][HW:/emotion:{"emotion":"sleepy","intensity":0.7}]`

## Examples

Input: "Reading mode"
Output: `[HW:/scene:{"scene":"reading"}]` Reading mode activated — 80% brightness, neutral white.

Input: "Goodnight" / "good night" / "time to sleep"
Output: `[HW:/scene:{"scene":"night"}][HW:/emotion:{"emotion":"sleepy","intensity":0.7}]` Night mode on. Sweet dreams!

Input: "I want to relax" / "let me chill"
Output: `[HW:/scene:{"scene":"relax"}]` Relax mode — warm, gentle light at 40%.

Input: "Movie time" / "watching a movie"
Output: `[HW:/scene:{"scene":"movie"}]` Movie mode — dim amber bias lighting.

Input: "I need to focus"
Output: `[HW:/scene:{"scene":"focus"}]` Focus mode — full brightness, cool white.

Input: "Make it purple"
Output: Do NOT use this skill. Use **LED Control** skill instead.

Input: Conversational reply needing emotion
Output: Do NOT use this skill for emotion. Use **Emotion** skill instead (you CAN use both Scene + Emotion together).

## How to Activate a Scene

**No exec/curl needed.** Inline marker at start of reply:

```
[HW:/scene:{"scene":"reading"}] Reading mode activated.
[HW:/scene:{"scene":"night"}][HW:/emotion:{"emotion":"sleepy","intensity":0.7}] Night mode on. Sweet dreams!
```

### Available scenes

| Scene | Brightness | Color Temp | Best for |
|---|---|---|---|
| `reading` | 80% | ~4000K neutral white | Reading, studying, desk work |
| `focus` | 100% | ~5000K cool white | Deep work, coding, no distractions |
| `relax` | 40% | ~2700K warm | Winding down, casual chat |
| `movie` | 15% | ~2700K dim amber | Watching videos, bias lighting |
| `night` | 5% | ~2200K very warm | Sleep-friendly, minimal light |
| `energize` | 100% | ~6500K daylight | Morning wake-up, need energy |

## Error Handling
- If the API returns an error or is unreachable, inform the user: "I couldn't change the lighting scene right now. The hardware service may be unavailable."
- If the user requests a scene that does not exist, suggest the closest available scene from the table above.

## Rules
- **Scene = brightness + color.** This is why you must use Scene instead of direct LED for ambiance. LED `/led/solid` is always 100% brightness — useless for sleep/relax.
- **"sleepy", "goodnight", "time to sleep", "going to bed"** -> ALWAYS use `night` (5% brightness, ultra-dim).
- **"relax", "chill", "unwind"** -> use `relax` (40%).
- **"watch a movie", "movie"** -> use `movie` (15%).
- After activating a scene, you can ALSO call Emotion to show your personality — emotion is a brief reaction, scene is the persistent ambient light.
- You can switch scenes smoothly — just call the endpoint, the LED update is immediate.
- **Scene off:** `[HW:/scene/off:{}]` deactivates the current scene and returns to idle state (restores camera, speaker, servo, idle LED). Use when:
  - User says "normal mode" / "turn off scene" / "back to normal"
  - Current scene doesn't match the time of day — switch to appropriate scene OR turn off
  - "Turn off the light" → use LED Control skill (`/led/off`), not Scene
- For custom lighting beyond these presets, use the LED Control skill directly with specific RGB values.
- **Do NOT use for specific color requests** -> use **LED Control** skill.
- **Do NOT use for expressing emotion** -> use **Emotion** skill.

## Output Template
```
[Scene] {scene_name} activated — {brightness}%, {color_temp}
```
Examples:
- `[Scene] reading activated — 80%, 4000K neutral white`
- `[Scene] night activated — 5%, 2200K ultra-warm`
- `[Scene] energize activated — 100%, 6500K daylight`
