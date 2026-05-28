---
name: user-emotion-detection
description: Maps a detected user emotion — from facial expression (emotion.detected) OR speech (speech_emotion.detected) — into a mood signal logged via the Mood skill, then picks one response route (music / checkin / action). This is about the USER's emotion (input), NOT Lamp's own expression — that's emotion/SKILL.md.
---

# User Emotion Detection

## Strict Trigger

Runs **only** when this turn's input has one of:

- `[emotion] Emotion detected: <EmotionName>` — facial expression from camera.
- `[speech_emotion] Speech emotion detected: <EmotionName>` — vocal expression from microphone.

Both are first-class triggers and route through the same logic below. The only differences are:
1. The mood signal `source` field — `"camera"` for `[emotion]`, `"voice"` for `[speech_emotion]`.
2. The label vocabulary — face uses `Fear/Surprise/Disgust`, voice uses `Fearful/Surprised/Disgusted`. The mapping table below covers both; they bucket identically.

For any other event (`[sensing:presence.*]`, `[activity]`, `[sensing:sound]`, `[sensing:light.*]`, …) → **STOP. No tool calls.** Don't infer emotion from older turns — every signal must come from a real `[emotion]` or `[speech_emotion]` event in THIS turn.

> **EXECUTE, don't plan.** Before any reply (including `NO_REPLY`) you MUST embed a `[HW:/mood/log:{...}]` marker in the reply for this event (it fires the POST async via the runtime — see "What this skill produces" below). Writing *"I will post a mood signal"* in `thinking` is **not** enough — if no `[HW:/mood/log:...]` marker appears in the reply text this turn, the skill failed. Every `emotion.detected` / `speech_emotion.detected` event = at least one mood signal log. No exceptions, no skips, no "already logged recently" reasoning. (`curl` POST is the documented fallback only when the HW marker would break the body regex; do not use it as the default.)

> **This skill is silent.** It only logs mood data — no spoken reply needed. All steps stay in `thinking`. If you want to speak, follow normal sensing reply rules. NEVER narrate mood logging, mapping, or workflow steps in the reply.

## What this skill does

On every `[emotion]` or `[speech_emotion]` event, turn the detected user emotion into a mood signal. Log it via the Mood skill, then route a response (music / checkin / action) based on the pre-fetched context.

This skill does NOT:

- Fire `[HW:/emotion:…]` markers. Emotion expression is `emotion/SKILL.md`'s job, driven by conversation context — not auto-mapped from a sensor reading.
- Require a spoken reply. Whether to speak is decided by the normal reply rules (SOUL + sensing SKILL), not by this skill.
- Write to the wellbeing log. Wellbeing is for physical activity (drink/break/sedentary); emotions live in the mood log.

## Trigger

Either of:

```
[emotion] Emotion detected: <EmotionName>.            ← camera (face)
[speech_emotion] Speech emotion detected: <EmotionName>.  ← microphone (voice)
```

Face FER labels: `Happy`, `Sad`, `Angry`, `Fear`, `Surprise`, `Disgust`, `Neutral`.
Voice emotion2vec labels: `Happy`, `Sad`, `Angry`, `Fearful`, `Surprised`, `Disgusted`, `Neutral` (plus `Other`, `<unk>` — dropped upstream).

Both formats end with the same `<EmotionName>.` anchor; the same regex parses either one.

## Emotion → mood (for the signal log)

Both label vocabularies map to the same mood values — voice variants (`Fearful`, `Surprised`, `Disgusted`) bucket identically to their face counterparts (`Fear`, `Surprise`, `Disgust`).

| Detected emotion (face OR voice) | `mood` value to log |
|---|---|
| `Happy` | `happy` |
| `Sad` | `sad` |
| `Angry` | `frustrated` |
| `Fear` / `Fearful` | `stressed` |
| `Surprise` / `Surprised` | `excited` |
| `Disgust` / `Disgusted` | `frustrated` |
| `Neutral` | `normal` |

## What this skill produces

A single `kind=signal` row in the mood log, emitted as an HW marker at the start of your spoken reply (the runtime fires the POST async, no tool turn):

```
[HW:/mood/log:{"kind":"signal","source":"<camera|voice>","trigger":"<EmotionName lowercase>","mood":"<mapped_mood>","user":"<current_user>"}]
```

**`source` is decided by the event prefix on THIS turn:**
- `[emotion]` → `"camera"`
- `[speech_emotion]` → `"voice"`

Don't override based on prior turns or the `recent_signals` block; the prefix is authoritative.

`mapped_mood` comes straight from the `[emotion_context: ...]` block — do NOT look it up from the table on the fly. Every detected emotion in the mapping table gets logged (including `Neutral` → `normal`) — Mood needs the recency for decision synthesis. Use `"unknown"` when the context tag is missing.

**Do NOT use `curl` exec for this signal log** — see `mood/SKILL.md`'s "What to write" section for the rationale (HW marker is single-trip, curl burns a tool turn). If no `[HW:/mood/log:...]` marker appears in the reply this turn, the skill failed.

## Combined with mood + music-suggestion

The backend injects this turn with an `[emotion_context: {...JSON...}]` block that pre-computes everything the three skills need (this skill is the router, mood logs the decision, music-suggestion fires only when this router picks the `music` route). **Do NOT fire any read tool calls** — the data is already in the message.

Pre-fetched fields (use directly):
- `mapped_mood` — already maps this turn's `<EmotionName>` per the table above. This is the value to log as the signal mood. **You no longer need to look it up yourself.**
- `recent_signals`, `prior_decision`, `is_decision_stale` — feed `mood/SKILL.md`'s decision rules and this skill's routing table.
- `audio_playing`, `last_suggestion_age_min`, `audio_recent`, `music_pattern_for_hour`, `suggestion_worthy` — feed this skill's routing table (see **Response routing** below) and `music-suggestion/SKILL.md`'s genre pick.

Single combined plan, not three sequential workflows:

- **Decide locally** — apply mood decision rules from `mood/SKILL.md`; pick a route from the routing table below; if the route is `music`, evaluate genre from `music-suggestion/SKILL.md`.
- **Writes (batch in one bash with `&` + `wait`)** — POST mood signal (this skill), POST mood decision (mood), and on `music` or `checkin` route, POST the music-suggestion log (the shared cooldown channel).

### Fallback (only if `[emotion_context: ...]` is missing)

If the message has no context block (pre-fetch failed), fall back to the read batch from `mood/SKILL.md` and `music-suggestion/SKILL.md` (concurrent GETs in one bash via `& ... wait`).

Reply: routing decides the spoken reply (see next section). Never narrate the mapping, logging, or routing decision.

## Response routing (this skill is the router)

After logging the mood signal, pick **exactly one** response route. Read straight from `[emotion_context: ...]` — no extra tool calls. Apply top-to-bottom, first match wins:

| # | Condition | Route | What happens |
|---|---|---|---|
| 1 | `audio_playing == true` | **action** | LED-only ambient ack, no spoken reply. Emit `[HW:/emotion:{"emotion":"caring","intensity":0.4}]` + `NO_REPLY`. Music is already covering — don't talk over it. |
| 2 | `suggestion_worthy == true` AND (`is_decision_stale == false` OR fresh decision synthesized this turn) AND `last_suggestion_age_min ∉ [0, 7)` | **music** | See `music-suggestion/SKILL.md` for genre + phrasing + log marker. |
| 3 | anything else (cooldown active, mood not worthy, stale decision with no fresh synthesis, mapped_mood normal/frustrated, etc.) | **checkin** | See `reference/checkin.md` for phrasing + log marker. One soft open-ended line. |

Rules:

- **One route per turn.** Don't double-fire (e.g. music + checkin both). Pick the first matching row.
- **Cooldown only gates music, not checkin.** When `last_suggestion_age_min ∈ [0, 7)` the music branch is blocked (row #2 fails its third clause) and the event falls through to checkin (row #3). The agent still asks — it just doesn't suggest music back-to-back. The only `NO_REPLY` path is row #1 (active audio).
- **Output ownership:** `music` → produced by `music-suggestion/SKILL.md`. `checkin` → produced by `reference/checkin.md` (this skill). `action` → emitted inline by this router (the `[HW:/emotion:...]` marker in row #1).
- **Cooldown is shared** between music and checkin: both log via `music-suggestion/log` so `last_suggestion_age_min` reflects either channel.
- Never narrate the routing decision in the spoken reply.
- `Neutral` is filtered upstream at lelamp and never reaches this skill in practice; no special case needed here.

## Voice cue is weaker than camera cue

Speech emotion (`[speech_emotion]`) is noisier than facial expression on short utterances — emotion2vec flips between `sad / fearful / angry` within the same affective state. The hedge `(weak voice cue; ...; treat as uncertain, ...)` is baked into the message for that reason.

Practical rules:

- **Don't relax the cooldown.** Music suggestions still gate on `last_suggestion_age_min ∉ [0, 7)` regardless of source. A fresh voice signal does NOT reset the cooldown that a recent camera-driven music suggestion left.
- **Prefer Comfort/Invite phrasing on voice-only negative reads.** When the router falls through to checkin (row #3) and the trigger is `[speech_emotion]` with `bucket=negative`, lean toward Comfort/Invite rather than Ask — probing a maybe-misclassified utterance feels worse than acknowledging it.
- **Cross-modal reinforcement still applies.** If `recent_signals` shows the same `mapped_mood` from `source="camera"` in the last ~10 min and now voice fires the same mood, treat it as a confirmation — the Mood skill's decision synthesis already handles this; no extra logic here.
- **No skip-on-low-confidence.** Don't read `confidence=...` out of the hedge text and pre-filter; lelamp already enforced `confidence >= SPEECH_EMOTION_CONFIDENCE_THRESHOLD` before sending. Anything that reaches this skill is worth logging.
