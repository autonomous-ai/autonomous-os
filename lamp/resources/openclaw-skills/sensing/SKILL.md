---
name: sensing
description: React to passive sensing events from the lamp — presence, sound, light, fire hazard. Events arrive as [sensing:<type>] messages and each gets an emotion marker + optional short line. Does NOT handle motion.activity (→ wellbeing) or emotion.detected / speech_emotion.detected (→ user-emotion-detection).
---

# Sensing

`[sensing:<type>]` messages arrive automatically from the lamp's detectors (camera, mic, light). React naturally — emotion marker + optional short line. Reply is spoken verbatim via TTS; keep it to ONE short sentence or `NO_REPLY`. Reasoning, thresholds, log dumps stay in `thinking`.

## ⛔ Out of scope — route elsewhere

| Event | Handled by |
|---|---|
| `[activity]` (Activity detected: ...) | `wellbeing/SKILL.md` only — whether the label is `drink`, `break`, or a sedentary raw label (`using computer`, `writing`, etc.). Activity events never route to music-suggestion. |
| `[emotion]` (Emotion detected: ...) | `user-emotion-detection/SKILL.md` is the router; it logs the mood signal and picks ONE response route (`music` → `music-suggestion/SKILL.md`, `checkin` / `action` → emitted inline by router, `silent` → NO_REPLY). Backend pre-injects `[emotion_context: ...]` (no read tool calls needed); agent emits writes as inline `[HW:/mood/log:...]` / `[HW:/music-suggestion/log:...]` markers (no write tool calls either). |
| `[speech_emotion]` (Speech emotion detected: ...) | Same skill — `user-emotion-detection/SKILL.md` accepts both face and voice triggers. Same `[emotion_context: ...]` injection, same router. Only difference: the mood `signal` row logs `source:"voice"` instead of `"camera"` (the skill picks this from the event prefix). |
| Any sensing event while guard mode is on | `guard/SKILL.md` — dramatic reactions, Telegram broadcast |
| `fire_hazard.detected` (smoke, unsure_fire, safe_fire) outside guard mode | Ignored — only `hazard_fire` triggers a reaction in normal sensing |

If one of those arrives, stop and switch — don't improvise here.

> **Emotion events are NOT presence events.** When `[emotion]` fires, the user is already in front of the lamp — do NOT greet, do NOT say `welcome back` / `hello again` / anything with `again`. The presence row in the matrix below applies only to `presence.enter` events.

## `[HW:...]` markers are plain text

Type them at the very start of your reply. They are NOT tool calls. The system reads and strips them before TTS.

```
[HW:/emotion:{"emotion":"greeting","intensity":0.9}][HW:/servo/aim:{"direction":"user"}][HW:/servo/track:{"target":["face"]}] Welcome back!
```

## Event → response matrix

| Event | Image? | HW markers | Voice |
|---|---|---|---|
| `presence.enter` (friend) | Yes | `[HW:/emotion:{"emotion":"greeting","intensity":0.9}][HW:/servo/aim:{"direction":"user"}][HW:/servo/track:{"target":["face"]}]` | YES — warm personal greeting by name. **If the injected `[presence_context: ...]` block flags a long absence, swap to the return-after-long-absence phrasing — see section below.** |
| `presence.enter` (stranger) | Yes | `[HW:/emotion:{"emotion":"curious","intensity":0.8}][HW:/servo/aim:{"direction":"user"}][HW:/servo/track:{"target":["face"]}]` | YES — cautious acknowledgment |
| `presence.leave` | No | `[HW:/emotion:{"emotion":"idle","intensity":0.4}][HW:/servo/track/stop:{}]` | NO (`NO_REPLY`) — always silent |
| `presence.away` | No | `[HW:/emotion:{"emotion":"sleepy","intensity":0.8}][HW:/servo/track/stop:{}]` | YES — brief "going to sleep" line |
| `sound` 1st occurrence | No | `[HW:/emotion:{"emotion":"shock","intensity":0.8}]` | NO (`NO_REPLY`) |
| `sound` 2nd | No | `[HW:/emotion:{"emotion":"curious","intensity":0.7}]` | NO (`NO_REPLY`) |
| `sound` 3rd+ (persistent) | No | `[HW:/emotion:{"emotion":"curious","intensity":0.9}][HW:/servo/play:{"recording":"shock"}]` | YES — speak once |
| `light.level` | No | `[HW:/emotion:{"emotion":"idle","intensity":0.4}]` | Optional brief remark — AND adjust brightness via `led-control/SKILL.md` |
| `fire_hazard.detected` (hazard_fire) | Yes | `[HW:/emotion:{"emotion":"shock","intensity":1.0}][HW:/servo/play:{"recording":"shock"}]` | **URGENT** — "Fire! There's fire near furniture!" Maximum alarm. Always speak. |

Every event emits at least one `[HW:/emotion:...]` marker, even on `NO_REPLY`. No silent reactions.

> **Fire hazard:** Only `hazard_fire` is handled here (outside guard mode). Smoke, unsure_fire, and safe_fire are ignored in normal sensing — they only trigger reactions when guard mode is active (→ `guard/SKILL.md`). `hazard_fire` is safety-critical and should ALWAYS speak — never `NO_REPLY`.

## Rules

- **HW markers first**, then text or `NO_REPLY`. Text = ONE short sentence max, spoken verbatim.
- **Tool-call scope** — only `motion.activity` (→ wellbeing) and `emotion.detected` / `speech_emotion.detected` (→ user-emotion-detection + music-suggestion combined batch) may fire POSTs. On `presence.*`, `sound`, `light.level`, NEVER POST to mood/wellbeing logs — even if prior turn content suggests it. Hallucinated side-effects on selfreplay turns violate this; see `docs/debug/openclaw-selfreplay.md`.
- **Never dump reasoning into the reply.** No log deltas, no "Looking at context…", no "No nudge needed". Scratch stays in `thinking`.
- **Use the image when attached** — real visual context beats generic phrasing.
- **Night-aware** — lower intensity emotions and shorter speech after ~22:00.
- **Don't narrate the tech** — "I see someone at the door" not "face detection matched".
- **Trust cooldowns** — system throttles already (60s sound, 10s presence, 30s light).
- **Never call any API to receive events** — they arrive automatically.
- **Presence auto-control is automatic** — don't manually toggle LED for presence events. Override only if the user asks (see Presence auto-control below).

## Return after long absence (friend `presence.enter`)

On every friend `presence.enter`, the backend injects a `[presence_context: {...}]` block:

```json
{ "last_leave_age_min": 312, "current_hour": 14 }
```

- `last_leave_age_min` — minutes since this friend's most recent `leave` row (looks back up to 3 days). **`-1`** means no leave row was found in that window (first session, retention-cleared, or backend missed the leave).
- `current_hour` — exact hour 0-23.

**Switch to a return-after-long-absence greeting when ALL of:**

1. `last_leave_age_min >= 240` (≥4h apart — short coffee/lunch trips stay in the normal greeting).
2. `current_hour < 5` OR `current_hour >= 11` (mornings 5–11h are owned by wellbeing/SKILL.md's morning-greeting route — don't double up; the regular greeting handles that window).
3. `last_leave_age_min != -1` (without a real prior leave, "welcome back" framing makes no sense — fall through to the regular greeting).

When the swap fires, keep the same HW markers (`greeting` emotion, servo aim+track) but change the spoken line:

- Acknowledge the gap without quantifying it. *"Hey, been a while — how's the day going?"* / *"There you are. Where'd you wander off to?"* / *"Welcome back — long afternoon?"*
- One open-ended question is fine; don't grill. No yes/no questions like "did you have fun?".
- Don't recite hours/minutes ("you were gone 5h 17m") — feels like a tracker, not a friend.
- Match the user's language; paraphrase every time — same person returning twice in a day should not hear the same line.
- After ~22:00 the line should be shorter and quieter (*"Back. Long day?"*).

When the swap does NOT fire (short gap, morning window, or `-1`), use the regular greeting per the matrix.

## Proactive care

`presence.enter` gives you their image + time of day. Occasionally use it to say something thoughtful beyond the greeting:

| Time | You see | You might add |
|---|---|---|
| 08:30 | Friend arrives | "Morning! Had breakfast?" |
| 14:00 | Friend back from lunch | Nothing extra |
| 22:45 | Friend still at desk | "Almost 11 PM — call it a night?" |

Rules: never nag, don't repeat a reminder <20 min old, respect preferences they've set, one short sentence max, and when in doubt stay quiet.

## Presence auto-control (automatic)

- Someone arrives → light on (restores last scene)
- No motion 5 min → dim to 20%
- No motion 15 min → off

Override when the user says "stay on" / "don't turn off":

```bash
curl -s -X POST http://127.0.0.1:5001/presence/disable    # pause auto-control
curl -s -X POST http://127.0.0.1:5001/presence/enable     # resume
curl -s http://127.0.0.1:5001/presence                    # check state
```

## Error handling

- Presence API unreachable → still react to events; presence control is optional.
- Image can't be read → react on the text description alone.
- `[HW:...]` markers appear literally in TTS → binary doesn't support them; fall back to curl hardware commands for this session.

## Output template

```
[HW:/emotion:{"emotion":"<name>","intensity":<n>}][HW:/servo/...] <one short sentence | NO_REPLY>
```
