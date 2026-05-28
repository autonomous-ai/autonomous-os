---
name: mood
description: Tracks the USER's mood only — signals + synthesized decision from camera/voice/telegram. Do NOT use for emotion commands directed at Lamp ("show sad", "be happy", bare "sad now"); those go through emotion/SKILL.md and are never logged here. Music/wellbeing skills consume the latest decision.
---

# Mood

> **OUTPUT RULE — read this before you type anything to the user.**
>
> This skill is an internal workflow. **NEVER narrate it into your reply.** Forbidden in the reply text:
> - Section names or step numbers ("Step 1", "Workflow", "After Logging Decision", "Flow A").
> - Phrases like *"Now I follow…"*, *"Let me check…"*, *"Next step…"*, *"I'll log…"*.
> - Bullet lists re-hashing the mood history you just read (*"- Normal (15:00) — …"* / *"- Excited (16:00) — …"*).
> - The mood value itself as a label (*"Mood: sad"*, *"Decision: happy"*).
> - Any of the JSON / curl / timestamps from this skill.
>
> **Your reply text** to the user is at most ONE short caring sentence (or `NO_REPLY`). All the workflow, logging, and synthesis happen silently via tool calls — the user only hears what you would naturally say if you were truly noticing how they feel.

> **ALWAYS log.** `unknown` is a valid `user` value — log signals and decisions under `user: "unknown"` when `current_user` is unknown. Never skip logging because the user is unknown/unconfirmed; stranger mood still counts for Music decisions.

Mood is stored as two kinds of rows:

- **`signal`** — raw evidence from one source (camera action, voice tone, telegram message). Multiple per minute is fine.
- **`decision`** — your synthesized mood after looking at the recent signals + the previous decision. This is the row downstream skills (Music, Wellbeing) read.

**You are the synthesis.** The store does not fuse anything. Every time a signal comes in, you log it raw, then immediately read recent history and append a fresh decision row.

---

## Mood Values

happy, sad, stressed, tired, excited, bored, frustrated, energetic, affectionate, unwell, normal

`normal` is the baseline when nothing strong is going on (use it for decisions when signals are sparse or stale).

## Signal Sources

| Source | Examples |
|--------|----------|
| `camera` | facial action: laughing, crying, yawning, sneezing, hugging, kissing, headbanging |
| `voice` | tone: soft, raised, sigh, laugh, monotone |
| `telegram` | message text: "lots of bugs today", "I'm tired", "let's gooo" |
| `conversation` | inferred from a stretch of voice/chat over multiple turns |

### Camera action → signal mood (rule of thumb)

| Action | Mood |
|--------|------|
| laughing, singing | happy |
| crying | sad |
| yawning | tired |
| applauding, clapping, celebrating | excited |
| sneezing | unwell |
| hugging, kissing | affectionate |
| headbanging | energetic |

For voice/telegram, infer boldly from a single line ("work is killing me" → stressed). Trust your gut.

Skip only if: quoting someone else, or speaking purely hypothetically.

---

## What to read (pre-fetched on emotion.detected / speech_emotion.detected)

When this skill runs as part of the emotion pipeline — either `emotion.detected` (camera) or `speech_emotion.detected` (voice) — the backend injects an `[emotion_context: {...}]` block with everything you need pre-computed:
- `recent_signals` — array of `{age_min, mood, source, trigger}` for signals within the last 30 minutes.
- `prior_decision` — the most recent `kind=decision` row as `{mood, age_min}`, or `null`.
- `is_decision_stale` — boolean (`age_min >= 30` or no decision today).

**Do NOT GET `mood-history` again** in that case — use the context block.

When the skill runs from another path (voice/telegram-driven mood signal, no `[emotion_context:]` block), fall back to:

```bash
curl -s "http://127.0.0.1:5000/api/openclaw/mood-history?user=<name>&last=15"
```

This returns the full ordered list `{signal, decision}`; derive the same three fields locally. The GET should batch concurrently with any other reads in the same turn (no data dependency).

## Decision rules

Apply this judgment when synthesizing the fused mood:

1. **Stale baseline.** If the last decision is older than ~30 min and there are few recent signals → start from `normal`.
2. **Single strong signal.** If the only fresh evidence is one strong source (e.g. user just typed "I'm exhausted") → that wins.
3. **Conflicting signals across sources.** Camera says `happy` but telegram says `stressed` in the same window → trust the higher-bandwidth source. Words about feelings beat a momentary facial expression. Multiple aligned signals beat a single outlier.
4. **Reinforcement.** New signal matches the previous decision → keep the decision (still log a fresh row so downstream sees the timestamp move).
5. **Drift.** New signal is close-but-different (e.g. `tired` after a `stressed` decision) → shift, don't snap.

## What to write (HW markers — fire async, no tool turn)

Embed both rows at the start of your spoken reply as HW markers. The runtime parses them, fires the POSTs in parallel goroutines, and strips them before TTS speaks the rest.

**Signal row** (raw evidence):

```
[HW:/mood/log:{"kind":"signal","mood":"<mood>","source":"<camera|voice|telegram|conversation>","trigger":"<short reason>","user":"<name>"}]
```

**Decision row** (synthesized):

```
[HW:/mood/log:{"kind":"decision","mood":"<fused mood>","based_on":"<short summary>","reasoning":"<why>","user":"<name>"}]
```

Both markers can sit in the same reply (signal first, then decision is fine — they fire concurrently anyway). They use the same endpoint; `kind` in the body distinguishes them.

| Field | Required | Notes |
|-------|----------|-------|
| `kind` | Yes | `signal` or `decision` |
| `mood` | Yes | from the values list above |
| `based_on` | Decision only | e.g. `"3 signals last 20min + last decision (stressed, 18min ago)"` |
| `reasoning` | Decision only | one sentence, e.g. `"telegram complaints outweigh the smile from camera"` |
| `user` | No | omit to use current presence user |

**Do NOT use `curl` exec for these logs.** Each curl consumes a tool turn (~5-7s LLM-think on the result) for a side-effect with nothing to wait on. The HW marker path is single-trip.

**Regex caveat:** the marker body must not contain `}`. `based_on` / `reasoning` are usually plain English so this is rarely a problem; if a value would contain `}` use the curl fallback instead.

### Fallback (only if HW marker is rejected by the runtime)

```bash
curl -s -X POST http://127.0.0.1:5000/api/mood/log \
  -H 'Content-Type: application/json' \
  -d '{"kind":"signal","mood":"<mood>","source":"...","trigger":"...","user":"<name>"}'
curl -s -X POST http://127.0.0.1:5000/api/mood/log \
  -H 'Content-Type: application/json' \
  -d '{"kind":"decision","mood":"<fused>","based_on":"...","reasoning":"...","user":"<name>"}'
```

`source` is automatically set to `"agent"` for decisions; do not pass `source` or `trigger`.

---

## User field

- **Camera**: omit `user` — face recognition sets the current user. If you need to verify, query `GET http://127.0.0.1:5001/face/current-user` → `{"current_user": "<name>"}` (friend name, `"unknown"` for strangers-only, or empty string when nobody is present). Do NOT parse this out of `/face/cooldowns` — that endpoint is for the friend/stranger cooldown debug view, not for attribution.
- **Telegram**: extract from `[telegram:SenderName]`, lowercase.
- **Voice**: omit (logged as `unknown`).

---

## Rules

- **Always do both steps.** Logging only a signal without a decision leaves downstream skills reading stale moods. Logging a decision without a signal hides the evidence.
- **Invisible.** Never mention mood logging or this skill in your reply. Deflect naturally if asked.
- **One signal per real trigger.** Don't log the same yawn twice. Multiple distinct signals in a short window are fine and useful.
- **Strangers count.** Log for `unknown` users too — Music still suggests for them.
- **Decisions are cheap.** Even when the mood doesn't change, write a fresh decision row so the timestamp stays current. Downstream uses recency to know if a mood is still valid.

---

## Music suggestion handoff

On `emotion.detected` and `speech_emotion.detected` turns, `user-emotion-detection/SKILL.md` is the router — it picks one of `music / checkin / action / silent` and gates whether `music-suggestion/SKILL.md` fires this turn. Voice and camera share one cooldown and one decision row schema; the only thing that changes per modality is the `source` field on the raw signal row.

When the router picks `music` (decision mood is suggestion-worthy — `sad`, `stressed`, `tired`, `excited`, `happy`, `bored` — and audio is idle, cooldown clear, decision fresh), the decision POST and the music-suggestion POST share a single write batch — do not split them across tool turns.

Other moods (`frustrated`, `energetic`, `affectionate`, `unwell`, `normal`) take a non-music route (`checkin` / `action` / `silent` per the router table) and skip the music POST.

For `unknown` users — still suggest (speak only, no DM) on the `music` route. See `music-suggestion/SKILL.md` for details.

---

## Examples

**Camera detects yawn, no recent context:**

- GET history → only this one yawn signal, last decision was 2h ago → stale.
- Signal: `{"kind":"signal","mood":"tired","source":"camera","trigger":"yawning"}`
- Decision: `{"kind":"decision","mood":"tired","based_on":"1 fresh signal, no recent decision","reasoning":"single yawning signal after stale window"}`
- Music skill consumes `tired` from the same turn (suggestion-worthy).

**Telegram says "let's go!" but camera 5 min earlier said yawning:**

- GET history → recent signals: `tired (camera, 5min ago)`, `excited (telegram, just now)`. Last decision: `tired, 4min ago`.
- Apply rule 3 — words beat one yawn → shift toward excited.
- Signal: `{"kind":"signal","mood":"excited","source":"telegram","trigger":"let's go!"}`
- Decision: `{"kind":"decision","mood":"excited","based_on":"telegram excitement overrides 5min-old camera yawn","reasoning":"verbal enthusiasm is higher-signal than a single facial cue"}`
- Music skill consumes `excited`.

**Quiet evening, no recent signals, user just sat down:**

- No new signal — nothing to log.
- If a downstream skill asks for current mood and the last decision is >30 min stale, it will read `normal` after the next signal arrives.
