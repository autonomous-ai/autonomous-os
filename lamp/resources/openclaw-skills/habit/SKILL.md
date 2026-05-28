---
name: habit
description: Tracks and analyzes behavioral patterns (habits) for known users based on their wellbeing, presence, posture, and activity history. Use when answering questions about a user's routines ("What are Leo's habits?", "Has Leo been keeping to his routine?", "Notice anything about my patterns?"), or when invoked from wellbeing/SKILL.md or posture/SKILL.md on a threshold nudge to refresh patterns and provide habit-aware phrasing. Also feeds music-suggestion/SKILL.md with `music_patterns` for personalized genre. Habit does NOT fire its own standalone nudge — it enriches wellbeing/posture threshold-nudge phrasing.
---

# Habit Skill

Habits are **repeating behavioral patterns** derived from historical logs. This skill reads existing data (wellbeing, presence, mood, music, posture) to build patterns per user, then stores them for other skills to consume.

> **OUTPUT RULE:** Reply is spoken VERBATIM. ONE short caring sentence. All computation, pattern math, and log lookups stay in `thinking`. NEVER output timestamps, deltas, frequency counts, or reasoning in the reply. **(Exception: Flow E — open habit questions — see below.)**

## Data Sources (Input)

All data lives in `/root/local/users/{name}/`:

| Folder | File pattern | What it contains |
|---|---|---|
| `wellbeing/` | `YYYY-MM-DD.jsonl` | `drink`, `break`, sedentary labels, `enter`/`leave`, `nudge_*` events with timestamps |
| `mood/` | `YYYY-MM-DD.jsonl` | `signal` + `decision` rows with moods |
| `music-suggestions/` | `YYYY-MM-DD.jsonl` | suggestion history + accepted/rejected status |
| `posture/` | `YYYY-MM-DD.jsonl` | `posture_alert` (ergo-risk events from camera) + `nudge_posture` / `praise_posture` rows |

**User names** are lowercase folder names under `/root/local/users/`. Known users: `leo`, `chloe`, `gray`, `lily`. Strangers collapse to `unknown` — this is treated as a regular user with its own folder and its own habit patterns (aggregated across all strangers).

JSONL line example (wellbeing):
```json
{"ts": 1776657145.05, "seq": 4, "hour": 10, "action": "drink", "notes": ""}
```

## Storage (Output)

Computed patterns are stored per user at `/root/local/users/{name}/habit/patterns.json`.

Rebuild when:
- File does not exist yet
- File is older than 6 hours
- User explicitly asks about their habits

## What is a Habit?

A habit is a **time-anchored action** that repeats across multiple days. Strength labels:

| Frequency | Strength |
|---|---|
| < 0.50 | weak (skip for nudging) |
| 0.50 – 0.75 | moderate |
| > 0.75 | strong |

Habits require **at least 3 days of data** to form. With fewer days, skip proactive nudging.

## Workflow

| Flow | When to run | Details |
|---|---|---|
| **A — Build patterns** | Discovery / answering questions; wellbeing nudge | `reference/build-patterns.md` |
| **B — Habit match** | Helper for wellbeing/SKILL.md Step 3b | `reference/match-helper.md` |
| **C — Music personalization** | Build / consume `music_patterns` | `reference/music.md` |
| **D — Conversation intent logging** | Triggered from SOUL when user states intent NOW | inline below |
| **E — Open habit question** | User asks about someone's habits / patterns / routines | `reference/open-question.md` |

### D — Conversation intent logging (triggered from SOUL)

SOUL instructs Lamp to call this flow when user expresses intent for a daily activity NOW.

**Intent → action mapping:**

| User says | Action to log |
|---|---|
| "lunch", "dinner", "going to eat", "grab food" | `meal` |
| "coffee break", "grab a coffee", "getting coffee" | `coffee` |
| "good night", "going to sleep", "heading to bed" | `sleep` |
| "gym", "exercise", "workout", "going for a run" | `exercise` |

**How to log:**

```bash
curl -s -X POST http://127.0.0.1:5000/api/wellbeing/log \
  -H 'Content-Type: application/json' \
  -d '{"action":"meal","notes":"user said: going to lunch","user":"<current_user>"}'
```

**Rules:**
- Log silently — do NOT tell the user you're logging. Just respond naturally.
- Only log when user states intent NOW, not past tense or general talk.
- One log per intent per conversation turn — no duplicates.
- `notes` field stores the original phrase for debugging.

## API Calls

### Read wellbeing history (via API)
```bash
curl -s "http://127.0.0.1:5000/api/openclaw/wellbeing-history?user={name}&date=YYYY-MM-DD&last=100"
```

### Read from file directly (for multi-day analysis)
```bash
cat /root/local/users/{name}/wellbeing/YYYY-MM-DD.jsonl
```

Use direct file reads for multi-day pattern building (faster, no API pagination needed).

### Check today's activity (quick presence check)
```bash
curl -s "http://127.0.0.1:5000/api/openclaw/wellbeing-history?user={name}&last=50"
```

## Integration Points

**From `wellbeing/SKILL.md`:** When wellbeing's Step 3 fires a threshold nudge, it invokes Flow A (which self-throttles via the freshness guard). Flow A returns the current `wellbeing_patterns`; wellbeing uses any matching pattern for the nudge action to enrich Step 4's phrasing (e.g. *"you usually drink around now"*). No separate habit-only nudge — habit context piggybacks on the threshold nudge. This keeps bootstrap cost on the rare nudge path, not on every `motion.activity` tick.

**From `posture/SKILL.md`:** Same pattern. When posture decides to nudge AND its context block has `bootstrap_needed=true`, it invokes Flow A. Flow A returns `posture_patterns` (peak hour, side bias, typical risk) which the posture coach uses to phrase pattern-aware nudges (e.g. *"around this hour you usually slip"*).

**From `music-suggestion/SKILL.md`:** Read `habit/patterns.json` → `music_patterns`. If habit data exists and current hour matches, use preferred genre instead of default genre table.

## Minimum Data Requirements

| Purpose | Min days | Min occurrences |
|---|---|---|
| Habit detection | 3 | 2 |
| Proactive nudging | 5 | 3 |
| Music personalization | 3 | 2 accepted |

If data is insufficient: use default wellbeing thresholds / music genre table as fallback. Never fabricate patterns.

## Output Examples

**Nudge enrichment (Flow A → wellbeing Step 3b):**
- Habit break: *"You usually have water around now — everything okay?"*
- Habit confirmed: *"Back at your desk right on schedule. [chuckle]"* — only say this if it feels natural
- Music: *"It's your usual coding time — want some lo-fi?"*
- Posture: *"Around this hour you usually slip — sit up from the start."*
- When no data: silent (NO_REPLY) — never guess or fabricate habits

**Open habit question (Flow E):**
- Pattern mode: *"Leo usually arrives around 8:30 with breakfast, settles at the computer through the morning, and wraps up close to 5. Lo-fi tends to land between 2 and 4. Pretty steady the last week."*
- Narrative mode: *"I've only got two real days on Chloe so far — April 28 was an evening at the computer with a lot of water breaks, and April 29 ran late, working past midnight. Not enough days yet to call it a habit, but that's what I've seen."*
- Honest-gap mode: *"Honestly, I haven't seen Leo much lately — just one short session yesterday. The patterns I have are from two weeks ago, so I'd rather not pretend they're still true."*
