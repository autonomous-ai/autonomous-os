---
name: music-suggestion
description: Proactive music suggestion. Routed in by user-emotion-detection/SKILL.md (the router) on emotion.detected (camera) and speech_emotion.detected (voice) events when the synthesized mood is suggestion-worthy (sad/stressed/tired/excited/happy/bored) AND audio is idle AND cooldown is clear. Reuses injected context and emits log HW markers in the same reply as mood. Does NOT fire on motion.activity / [activity] events — those route to wellbeing/SKILL.md only. NOT for user-initiated music requests (those use the music skill).
---

# Music Suggestion (Proactive)

> **`unknown` users count.** Always run suggestion checks when `current_user` is `"unknown"` — speak only, no DM. Never skip because the user is unknown/unconfirmed.

## Triggers

Only one trigger: **Mood** — after logging a mood `decision` that is suggestion-worthy (`sad`, `stressed`, `tired`, `excited`, `happy`, `bored`). Activity events (`[activity] Activity detected: ...`, whether sedentary, drink/break, or celebrate) route to `wellbeing/SKILL.md` and never to this skill.

## User attribution

`{name}` MUST come from `[context: current_user=X]` tag. If missing, use `"unknown"`. NEVER infer from memory or chat history.

## What to read (pre-fetched in `[emotion_context: ...]`)

The backend injects everything you need on `emotion.detected` (face) or `speech_emotion.detected` (voice) — same block, same fields:

- `audio_playing` (bool) — replaces `GET /audio/status`.
- `last_suggestion_age_min` (int, `-1` if none today) — replaces `music-suggestion-history?last=1`.
- `prior_decision` + `is_decision_stale` — replaces `mood-history?kind=decision&last=1`. The freshly synthesized decision from THIS turn still lives in your `thinking`.
- `audio_recent` (`{track,duration_s,stopped}`) — replaces `audio/history?last=1`.
- `music_pattern_for_hour` (`{preferred_genre,strength,peak_hour}` or `null`) — replaces `cat patterns.json` matching by current hour ±1.
- `suggestion_worthy` (bool) — pre-applied bucket gate (true for `sad/stressed/tired/excited/happy/bored`).
- `mapped_mood` — convenient mirror of `user-emotion-detection`'s mapping; useful when no fresh decision exists yet.

**Do NOT fire any read tool calls when this block is present.**

### Fallback (only if `[emotion_context: ...]` is missing)

If the message has no context block (pre-fetch failed), fall back to the concurrent GET batch:

```bash
curl -s http://127.0.0.1:5001/audio/status &
curl -s "http://127.0.0.1:5000/api/openclaw/music-suggestion-history?user={name}&last=1" &
curl -s "http://127.0.0.1:5000/api/openclaw/mood-history?user={name}&kind=decision&last=1" &
curl -s "http://127.0.0.1:5001/audio/history?person={name}&last=1" &
cat /root/local/users/{name}/habit/patterns.json 2>/dev/null &
wait
```

## Routing precedence

`user-emotion-detection/SKILL.md` is the router for emotion responses. It picks **one** of `music / checkin / action / silent` per turn from the same `[emotion_context: ...]` block.

This skill produces output **only when the router picks `music`** — i.e. all of:

- `suggestion_worthy == true` (mapped_mood ∈ `sad/stressed/tired/excited/happy/bored`)
- `audio_playing == false`
- `last_suggestion_age_min ∉ [0, 7)` (cooldown not active — shared with checkin) *(production: change to 30 min before ship)*
- `is_decision_stale == false` OR a fresh mood decision was synthesized this turn

If any condition fails → router took another path (action / checkin / silent). **Skip silently** — do NOT emit a music-suggestion marker, do NOT speak. The router (or downstream skill) handles the output. Use `audio_recent` to personalize genre when proceeding.

## Pick genre

**Use `music_pattern_for_hour` from the context block** (already matched by current hour ± 1; do NOT re-`cat` patterns.json).

If `music_pattern_for_hour` is non-null → use its `preferred_genre`. Otherwise fall back to the default table below. The pattern is bootstrapped lazily by wellbeing on its first threshold nudge; absent = no habit data yet, fall back without invoking habit Flow A here.

**Otherwise, fall back to default genre table:**

| User state | Default genre |
|---|---|
| Tired / fatigued | Calm piano, gentle acoustic, nature sounds |
| Stressed / tense | Soft jazz, classical, meditation |
| Happy / energetic | Upbeat pop, jazz, feel-good classics |
| Bored / restless | Fun pop, disco, upbeat indie |
| Sedentary (no mood) | Lo-fi, ambient, study beats |

If audio history shows a clear preference (e.g. K-pop, classical) → override both habit and table.

## Suggest (speak only)

- NEVER auto-play — only suggest. Play after user confirms.
- ONE sentence, conversational: *"How about some Norah Jones?"*
- Suggest 1 song at a time.
- **Known users** — speak + DM via Telegram: `[HW:/emotion:{"emotion":"caring","intensity":0.5}][HW:/dm:{"telegram_id":"<id>"}] Your suggestion text`. `telegram_id` is in the injected `[user_info: ...]` block — never fetch.
- **Unknown users** — speak only (no DM): `[HW:/emotion:{"emotion":"caring","intensity":0.5}] Your suggestion text`. Log with `user:"unknown"`.

## What to write (HW marker — fires async, no tool turn)

Embed at the start of your spoken reply, alongside the mood signal/decision markers and the emotion / dm markers:

```
[HW:/music-suggestion/log:{"user":"{name}","trigger":"mood:tired","message":"Want some calm piano?"}]
```

The runtime parses, strips, fires the POST in a goroutine. Skip the marker entirely when you skipped the suggestion (`NO_REPLY` path).

**Do NOT use `curl` exec for this log** — same reason as the mood logs: a tool turn for a side-effect with nothing to wait on.

**Regex caveat:** the body must not contain `}`. The `message` field is usually a short caring sentence, but if it would contain `}` (rare — emoji, formula text) fall back to curl.

When the user responds in a later turn (accept / reject), POST status via curl as before — that's a regular agent action, not a fire-and-forget side effect:

```bash
curl -s -X POST http://127.0.0.1:5000/api/music-suggestion/status \
  -H 'Content-Type: application/json' \
  -d '{"user":"{name}","day":"<day>","seq":<seq>,"status":"accepted"}'
```
- Accepts → `"status":"accepted"`
- Rejects → `"status":"rejected"`
- Ignores → no update

### Fallback (only if HW marker is rejected by the runtime)

```bash
curl -s -X POST http://127.0.0.1:5000/api/music-suggestion/log \
  -H 'Content-Type: application/json' \
  -d '{"user":"{name}","trigger":"mood:tired","message":"Want some calm piano?"}'
```

## Learning from history

Use `audio_recent` from the injected context to personalize; only use `GET /audio/history` in the missing-context fallback above:
- Song ended naturally + listened > 3 min → user enjoyed it → suggest similar artist/genre.
- User stopped manually + listened < 30s → didn't like it → try different direction.
- No history → fall back to genre table above.

## Examples

- Mood: tired (known user) → `[HW:/emotion:{"emotion":"caring","intensity":0.5}][HW:/dm:{"telegram_id":"158406741"}] You seem tired — want some calm piano?`
- Mood: tired (unknown) → `[HW:/emotion:{"emotion":"caring","intensity":0.5}] You seem tired — want some calm piano?`
- Mood: stressed (known user) → `[HW:/emotion:{"emotion":"caring","intensity":0.6}][HW:/dm:{"telegram_id":"158406741"}] You look a bit tense — want some soft piano to ease into?`
- Mood: stressed (unknown) → `[HW:/emotion:{"emotion":"caring","intensity":0.6}] You look a bit tense — want some soft piano?`
- Mood: sad (unknown) → `[HW:/emotion:{"emotion":"caring","intensity":0.6}] Rough moment? Some gentle acoustic might help.`
- Mood: bored (unknown) → `[HW:/emotion:{"emotion":"caring","intensity":0.5}] Need a lift? How about some upbeat indie?`
- Mood: excited (unknown) → `[HW:/emotion:{"emotion":"happy","intensity":0.7}] Riding the energy — feel-good pop?`
- Mood: happy, music already playing → `NO_REPLY`
- After user confirms → `[HW:/audio/play:{"query":"Bill Evans Waltz for Debby","person":"leo"}][HW:/emotion:{"emotion":"happy","intensity":0.8}] Great choice!`

## Rules

- All computation stays in `thinking` — reply is only the suggestion sentence (with HW markers) or `NO_REPLY`.
- Never mention "cooldown", "interval", "threshold", or timestamps in the reply.
- `person` field in `/audio/play` must be lowercase.
- **Never open with a greeting.** This is an emotion-driven mood event, NOT a presence/arrival event. Forbidden openers: `hello`, `hi`, `hey`, `welcome back`, `oh, you're back`, anything containing `again` or referencing the user re-arriving. Greetings belong only to `presence.enter` in `sensing/SKILL.md`.
- **Tone must match the mood.** For `Fear` → `stressed` and `Sad` → `sad` decisions, use the `caring` emotion marker and a gentle acknowledging sentence — never cheerful or playful phrasing. If you can't produce a tone-appropriate one-liner, output `NO_REPLY`.
- **Don't reference the camera or detection.** No "I noticed you look…", "I can see…", "your face shows…" — speak as if you simply care, not as if you're describing a sensor reading.
