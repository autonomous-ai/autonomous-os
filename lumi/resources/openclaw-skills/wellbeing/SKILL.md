---
name: wellbeing
description: Proactive coaching across hydration, breaks, meals AND posture. Use when an [activity] event fires (message starts with `[activity] Activity detected: <labels>.` — labels include drink, break, or sedentary raw labels like "using computer"; sedentary events may also carry a [posture_summary: {...}] block when the user has been at the computer long enough for posture to drift), or when the user asks if they should drink water / take a break / fix their posture. Thresholds are computed from per-user logs, never guessed.
---

# Wellbeing

## Gotchas (concrete facts, NOT suggestions)

**Endpoints — use verbatim, never substitute a port or path:**

| Purpose | URL |
|---|---|
| Read wellbeing history | `http://127.0.0.1:5000/api/openclaw/wellbeing-history` |
| Log wellbeing nudge | `http://127.0.0.1:5000/api/wellbeing/log` |
| Log posture nudge | `http://127.0.0.1:5000/api/posture/log` |

- Port **5000** = Lumi (data APIs: wellbeing / posture / mood / music / openclaw history).
- Port **5001** = LeLamp HARDWARE (audio, camera, face, presence, speaker). Has **NO** `/api/wellbeing/*` or `/api/posture/*` routes — calling 5001 returns 404 silently and your nudge is lost.
- Posture nudges live on a **separate JSONL** (`/root/local/users/<user>/posture/`) for clean timeline separation; the wellbeing log keeps hydration/break/meal/sleep/morning rows.
- Do not pattern-match from other skills: `5001/audio/play`, `5001/face/enroll`, `5001/camera/snapshot` are unrelated.

**User attribution:** every `user` field MUST come from the `[context: current_user=X]` tag the backend injects into the triggering event. Strangers collapse to `"unknown"`. If no context tag is present, default to `"unknown"`.

**Thresholds (production values):**

```
HYDRATION_THRESHOLD_MIN = 45
BREAK_THRESHOLD_MIN     = 30
TOILET_DRINK_THRESHOLD  = 2     # count-based — fires once per N drinks since last nudge
```

**LeLamp writes activities; you only write nudges.** Rows for `drink` / `break` / sedentary labels are posted by LeLamp directly when `motion.activity` fires — before the event reaches you. Do NOT re-log them. You still POST `nudge_hydration` / `nudge_break` because only you know when you actually spoke.

**Presence rows** (`enter` / `leave`) are written by the backend on `presence.*` events. You never POST those either.

## Rules (Never / Only)

1. **Only** call `http://127.0.0.1:5000/api/openclaw/wellbeing-history` to read history. **Never** read `/root/local/users/*/wellbeing/*.jsonl` or `/root/local/users/*/posture/*.jsonl` with `cat`, `ls`, `head`, `tail`, `grep`, or any filesystem tool. Posture history is digested into `last_posture_nudge_age_min` upstream — no agent-side read is needed.
2. **Only** POST to `http://127.0.0.1:5000/api/wellbeing/log` (hydration / break / toilet / morning / sleep / meal) or `http://127.0.0.1:5000/api/posture/log` (nudge_posture / praise_posture). **Never** substitute `5001`, `8080`, or any other port. **Never** omit `http://` or hardcode `localhost`.
3. **Only** write these action values: `nudge_hydration`, `nudge_break`, `nudge_toilet`, `morning_greeting`, `sleep_winddown`, `meal_reminder` (wellbeing log), `nudge_posture`, `praise_posture` (posture log). Never invent new actions. (Activity rows — `drink`, `break`, raw sedentary labels, raw eat labels like `eating burger` / `dining` / `tasting food` — are written by LeLamp, never by you. There are no agent-written `posture_alert` rows; LeLamp's per-frame samples live in a separate debug JSONL on the lamp and never reach the posture history API.)
4. On a non-2xx response from a POST → you used the wrong port or path. Fix the URL and retry **once**. Do not give up silently — the nudge row must land, or the skill will spam reminders forever.
5. **Never** infer `user` from memory, `KNOWLEDGE.md`, chat history, or `senderLabel`. Only the `[context: current_user=X]` tag counts.
6. **Trust the log, not memory.** If the history response contains no `nudge_hydration` entry, no nudge has happened — ignore any self-memory claim otherwise.

## Read pre-fetched context (do not re-fetch)

The backend injects a `[wellbeing_context: {...JSON...}]` block into every
motion.activity turn. Sedentary turns may also carry `[posture_summary: {...}]`
+ `[computer_streak_min: N]` blocks (see `reference/posture.md`). **Do NOT
fire any tool calls to re-fetch this data.** Saves the entire read tool turn.

Schema (every field is pre-computed in Lumi Go — agent only applies thresholds and picks phrasing):

```json
{
  "hydration_delta_min": 8,        // minutes since last drink/enter/nudge_hydration; -1 if no reset today
  "break_delta_min": 23,           // minutes since last break/enter/nudge_break; -1 if no reset today
  "latest_activity": "using computer",  // most recent action label (sedentary or reset); "" if no events
  "count_today": {"drink": 3, "break": 1},  // tally of reset actions today; missing key = 0; whole field omitted if all zero
  "time_of_day": "afternoon",      // morning|noon|afternoon|evening|night — coarse bucket for reaction flavor
  "current_hour": 14,              // exact hour 0-23 — used by activity router for hour-based routes
  "first_activity_today": false,   // true when no prior REAL user activity events today (presence enter/leave and agent-written nudges/reminders are NOT counted)
  "meal_window": "",               // "lunch" (11:30-13:30) | "dinner" (18:30-20:30) | "" — set by current_hour
  "meal_signal_in_window": false,  // true when a meal signal (meal_reminder Lumi already fired OR a raw eat label like "eating burger" / "dining" LeLamp logged) already exists in the current window today
  "morning_greeting_done_today": false,     // true when a morning_greeting action exists today
  "sleep_winddown_done_today": false,       // true when a sleep_winddown action exists today
  "drinks_since_toilet_nudge": 2,           // count of `drink` rows logged after the most recent `nudge_toilet` today (or all today's drinks if none yet); counter resets the moment you POST `nudge_toilet`
  "patterns": {                    // wellbeing patterns from patterns.json (mtime < 6h, strength >= moderate); omitted if none
    "drink": {"typical_hour": 9, "typical_minute": 15, "strength": "moderate"}
  },
  "bootstrap_needed": false,       // true → patterns missing/stale AND days >= 3; only invoke habit Flow A when also nudging
  "last_posture_nudge_age_min": 12 // minutes since the most recent nudge_posture today; -1 if none. Used by posture-nudge / praise routes — see reference/posture.md
}
```

Notes:
- Delta = `-1` means no reset action has happened today yet → treat as "no nudge" (delta undefined).
- `count_today` is for **reaction phrasing only** ("N-th drink today", streak callouts). It does NOT decide whether to speak — that's the trigger labels in the activity message.
- `patterns` only surfaces moderate/strong matches. Weak patterns are filtered out by the backend.
- `bootstrap_needed=true` does NOT mean run Flow A unconditionally — only if THIS turn fires a nudge.

### Posture summary (only on long sedentary streaks)

When the user has been sitting + bad-postured long enough, the activity
message carries `[posture_summary: {...}]` + `[computer_streak_min: N]`
blocks. See `reference/posture.md` for the schema, when LeLamp attaches
it, and the decision logic. The wellbeing context's
`last_posture_nudge_age_min` corroborates whether a nudge already fired
recently (defends against double-nudge if lelamp restarted).

### Fallback (only if context block is missing)

If the message has no `[wellbeing_context: ...]` block (pre-fetch failed), fall back to the bash batch:

```bash
{
  echo '---history---'
  curl -s "http://127.0.0.1:5000/api/openclaw/wellbeing-history?user=<current_user>&last=50" | jq '.data.events' &
  echo '---patterns---'
  PATTERNS=/root/local/users/<current_user>/habit/patterns.json
  if [ -f "$PATTERNS" ] && [ $(( $(date +%s) - $(stat -c %Y "$PATTERNS") )) -lt 21600 ]; then
    cat "$PATTERNS"
  fi &
  echo '---days---'
  ls /root/local/users/<current_user>/wellbeing/*.jsonl 2>/dev/null | wc -l &
  wait
}
```

In the fallback path, compute deltas yourself by scanning `history` for the latest reset action.

## Decision rules (activity router)

Read the `[activity] Activity detected: <labels>.` message + the `[wellbeing_context: ...]` block, then pick **exactly one** route. Apply top-to-bottom, first match wins. Reaction outranks everything — the user just acted; routing past it would feel tone-deaf.

| # | Condition | Route | Output |
|---|---|---|---|
| 1 | labels list contains `drink` or `break` OR any raw eat label (`eating burger`, `dining`, `tasting food`, … — i.e. any `eating *` / `dining` / `tasting food`) | **reaction** | 1–3 sentence acknowledgment per the **Reaction** section. **No HW marker** (LeLamp already logged the row upstream). |
| 2 | `first_activity_today == true` AND `current_hour ∈ [5, 11)` AND `morning_greeting_done_today == false` | **morning-greeting** | See `reference/morning-greeting.md`. Logs `morning_greeting` action to gate next firings today. |
| 3 | `current_hour >= 21` AND labels are sedentary (no `drink`/`break`) AND `sleep_winddown_done_today == false` | **sleep-winddown** | See `reference/sleep-winddown.md`. Logs `sleep_winddown` action. Replaces break nudge in late evening. |
| 4 | `meal_window` is non-empty AND `meal_signal_in_window == false` | **meal-reminder** | See `reference/meal-reminder.md`. Logs `meal_reminder` action with trigger `lunch` / `dinner`. Gate covers BOTH a prior reminder Lumi already fired AND a real eat label LeLamp logged — so we don't ask "have you eaten?" after a real meal. |
| 5 | `[posture_summary]` block present in the message | **posture-nudge** | Speak a posture nudge per the **Posture phrasing** section + post `nudge_posture` to the **posture log** (NOT wellbeing log). Anchor the line on `dominant_region` and `streak_min`. Outranks plain break/hydration nudges so we don't double-up on "stand up". |
| 6 | `hydration_delta_min >= HYDRATION_THRESHOLD_MIN` | **hydration-nudge** | Speak a hydration nudge per the **Phrasing** section + post `nudge_hydration` HW marker. |
| 7 | `break_delta_min >= BREAK_THRESHOLD_MIN` | **break-nudge** | Speak a break nudge + post `nudge_break` HW marker. |
| 8 | `drinks_since_toilet_nudge >= TOILET_DRINK_THRESHOLD` | **toilet-nudge** | Speak a toilet nudge per the **Phrasing** section + post `nudge_toilet` HW marker. The POST resets the counter to 0 → next nudge only after another full N drinks. |
| 9 | anything else (sedentary under threshold, or any delta == `-1` → no reset today yet) | **silent** | `NO_REPLY`. |

**Rules:**

- **One route per turn.** Pick the first matching row, then stop.
- **Reference files own the phrasing** for routes #2–#5 (morning-greeting / sleep-winddown / meal-reminder / posture-nudge). The corresponding HW marker logs `action=<route name>` so the next event in the same window/day sees `*_done_today` / `*_done_this_window` true and skips re-firing.
- The `nudge_*` row you POST in routes #6 (hydration) / #7 (break) acts as the next reset point for `hydration_delta_min` / `break_delta_min`, so once you nudge the delta drops to 0 and the next reminder of that kind only fires after another full threshold window. Route #8 (toilet) similarly resets `drinks_since_toilet_nudge` to 0 on POST.
- Route #5 (posture) does NOT follow that pattern — re-firing is gated by lelamp's `POSE_NUDGE_COOLDOWN_S` (~30 min, enforced upstream by suppressing the `[posture_summary]` block). Your POST does not by itself reset a timer; if the block is absent, you cannot nudge.
- Never narrate the routing decision in the spoken reply.

## Reaction (when the user just did the thing)

When the activity labels include `drink`, `break`, or any raw eat label (`eating burger`, `dining`, `tasting food`, …), **always speak** — silence on a positive action makes Lumi feel dead. This is the path the user explicitly asked for: short, surprised, casual acknowledgments instead of stoic NO_REPLY.

**Inputs to weave in (use what fits, ignore what doesn't):**
- `count_today.drink` / `count_today.break` — N-th of the day, streak, milestone.
- `time_of_day` and `meal_window` — morning kickoff, mid-afternoon dip, lunch time, late at night.
- `hydration_delta_min` / `break_delta_min` — small (e.g. 2) = back-to-back, big (e.g. 90) = first in a long gap.
- The raw activity label that came alongside (e.g. `drink, using computer` → comment on hydrating mid-screen-time; `eating burger` → comment on the specific food).

**Form:**
- 1–4 sentences, conversational, slightly playful or surprised — NOT a nudge, NOT advice. Length should follow the moment: a quick *"Nice."* is fine; a longer riff is fine too when there's something to riff on (a milestone count, a funny pairing of label + time-of-day, a streak).
- It's OK to weave in a tiny health-context aside if it fits naturally (*"eyes will thank you"*, *"kidneys say thanks"*) — one short clause, never a lecture, and never the same line twice in a row.
- Match the user's spoken language (Vietnamese in / Vietnamese out, English in / English out).
- **No `[HW:...]` marker.** Reactions don't log; the underlying `drink` / `break` row was already written by LeLamp.

**Variety is non-negotiable.**

The same `drink` + same count + same time-of-day will reach you many times in a single day. **Never repeat a reaction sentence verbatim, and don't lean on the same opener twice in a row.** A canned-feeling "I'm noticing you" loop is the exact failure mode this section exists to prevent.

You have the conversation context — *use it*. Look at what you said in your last few reactions this session and intentionally diverge: different opener, different angle (count vs. timing vs. mood vs. the sedentary label paired with it), different sentence length, different register. A smart agent self-checks against its recent output before speaking. A dumb agent re-runs the template. Be the former.

If you genuinely cannot think of a fresh angle, prefer a shorter line ("Nice.") over recycling.

**Example tones — illustrations only, never copy verbatim:**

- *"Whoa, third drink today already — staying on top of it."*
- *"Just sipped, going again — thirsty?"*
- *"End of day and that's your first one — grab another while you're at it."*
- *"Mid-afternoon break. Nice."*
- *"Two breaks already this morning — pacing yourself nicely."*
- *"Drink number five, that's the most you've had today. Keep it up."*
- *"Late-night sip. Keep it short and back to bed soon yeah?"*
- *"Burger looks good — enjoy it."* (raw label `eating burger`)
- *"Dining mid-lunch — right on time."* (raw label `dining` in lunch window)
- *"Spaghetti this late? Bold move."* (raw label `eating spaghetti`, evening)
- *"Cake between meetings — celebrating something?"* (raw label `eating cake`, off-meal)

After speaking, you are done — no log POST, no extra tool calls, no follow-up question unless something is genuinely off (e.g. 8th drink in an hour).

## Habit refresh (only when a nudge will fire)

If you decided to nudge AND the context block has `bootstrap_needed=true` → invoke `habit/SKILL.md` Flow A in a separate tool turn to bootstrap `patterns.json` from the multi-day log. Otherwise, **do not load `habit/SKILL.md`** — the `patterns` field in the context block is sufficient (or no patterns yet, that's fine).

Bootstrap is rare (file already exists for active users); the common path is "patterns object present → use it directly".

If the context's `patterns` map has an entry for the action you are about to nudge:
- Match `(action == nudge_target, now within typical_hour:typical_minute ± ~30min)` → weave it into the speech (*"you usually drink around now — everything okay?"*).
- No matching pattern (or `patterns` omitted) → use generic phrasing.

If you decided NOT to nudge (`NO_REPLY`) → never invoke Flow A. Habit bootstrap piggybacks on real nudge events, not idle motion ticks.

Posture nudges (route #5) do NOT trigger habit Flow A — the `patterns` field in `[wellbeing_context]` is keyed on `drink` / `break` reset actions only, and `bootstrap_needed` is computed from the wellbeing log alone.

Example: *hydration nudge fires at 9:15am, patterns.json says drink @ hour=9 typical_minute=10 → "you usually have water around now — grab a glass?"*

## Phrasing (when nudging)

**Talk like a friend, not a wellness app.** The historic rule was "1–2 short sentences" — that produced canned, stiff nudges. Use **2–4 sentences** now, with room for an observation, the ask, and a light reason or playful jab. Short is still allowed if the moment calls for it (e.g. user just spoke and you're piggybacking). The point is variety and warmth, not a fixed length.

**Weave in a health-context line when it fits** (one short clause, never a lecture):
- **Hydration** → energy / focus / dry-eye / headache. *"…a sip will keep the afternoon fog off."*
- **Break** → eye strain, neck, wrists, blood flow. *"…give your eyes and neck a beat off."*
- **Toilet** → kidneys, bladder. *"…holding it for hours is rough on the kidneys."*
- **Meal** → blood sugar / afternoon energy. *"…so you've got fuel for the rest of the day."*
- **Sleep wind-down** → sleep quality, tomorrow's energy. *"…you'll thank yourself in the morning."*

Use **at most one** health line per nudge. If the same user got the same health line in this session, switch to a different angle (observation, count, time-of-day, playful) instead. Health framing is seasoning, not the dish.

**⛔ Never speak a table row verbatim.** The tables below show **tone** (observation + soft question + optional reason), not a script. Paraphrase every turn — even if the activity is the same as last time. A canned-feeling loop is the exact failure mode this section exists to prevent.

**Variety self-check before speaking:**
- Look at your last 2–3 nudges this session. Different opener? Different angle (observation vs. health vs. count vs. timing vs. playful)? Different sentence count?
- If you genuinely can't think of a fresh angle, prefer **shorter and casual** ("Water." / "Up on your feet for a sec.") over recycling a template.

Ground each phrasing in the current raw label from the `Activity detected:` line so the nudge feels observed, not generic.

### Posture tone (see `reference/posture.md`)

Anchor on streak length (rounded — *"nearly an hour"*, never the exact
number), `dominant_region`, one concrete fix, optional health-context
clause. Side prefix only for arm/wrist when L/R sub-scores differ ≥ 2.
Full tone table + health-framing guardrails + praise route + HW marker
shape live in `reference/posture.md`.

### Hydration tone (paraphrase — never copy)

| Raw label | Example tone |
|---|---|
| `using computer` | *"Eyes have been glued to that screen a while. Quick sip of water before your head starts to ache — your brain runs on hydration, not just caffeine."* |
| `writing` | *"Pen's been moving non-stop. Grab a glass — staying hydrated keeps your thinking sharper than another coffee would."* |
| `texting` | *"Phone's had your full attention for a while. Got water nearby?"* |
| `reading book` | *"Deep in it, I see. Sip of water before the next chapter — dry eyes pull you out faster than a bad sentence."* |
| `reading newspaper` | *"Pages have been turning a while. Water alongside before the next one?"* |
| `drawing` | *"You're in the zone. While your hand's moving, get some water in — easier to keep the flow going than to push through a dry spell."* |
| `playing controller` | *"Mid-session, I won't pull you out — just keep water within arm's reach. Dehydration drags reaction time more than you'd think."* |
| (no label) | *"Haven't seen you drink anything in a while. A glass of water sounds about right — even a small one counts."* |

### Break tone (paraphrase — never copy)

| Raw label | Example tone |
|---|---|
| `using computer` | *"You've been on that screen a while. Look up at the ceiling for twenty seconds, roll your neck a bit — your eyes will thank you."* |
| `writing` | *"Hand's been writing for ages. Stand up, take a thirty-second walk, let the blood move again before you head back in."* |
| `texting` | *"Neck's been bent down forever — that catches up with you later. Stand up and stretch your shoulders for a sec."* |
| `reading book` | *"You've been reading straight through. Close your eyes for ten seconds or look out the window — give them a reset."* |
| `reading newspaper` | *"Eyes have been working hard. Glance out the window for a moment, just to let them rest."* |
| `drawing` | *"Hands and shoulders have been working overtime. Drop the pen for thirty seconds, shake out your wrists — stiff hands ruin clean lines."* |
| `playing controller` | *"Wrap up this round, then stand and stretch your legs. Sitting still tightens up your circulation — you'll feel it tonight if you don't."* |
| (no label) | *"You've been parked in one spot a while. Up on your feet for a quick lap, get the body waking up again."* |

### Toilet tone (new — paraphrase — never copy)

This only fires after several drinks, so the natural opener is "you've had a fair bit to drink already". Keep it casual — bathroom talk isn't shy, but it isn't a lecture either.

| Vibe | Example tone |
|---|---|
| caring | *"You've had a fair bit to drink already. Take a quick bathroom run — holding it stresses your kidneys, no need to push through."* |
| playful | *"That's {count} drinks in and you're still glued to that chair — your bladder's tagging you in. Stand up, you'll feel lighter."* |
| straightforward | *"You've drunk a lot today — go take a bathroom break, your kidneys will thank you. Holding it for hours isn't doing them any favors."* |
| gentle | *"All that water's probably catching up about now. Pop up for a sec — your seat's not going anywhere."* |

If `count_today.drink` is in context, weave it in concretely (*"that's four drinks already"*). If not, just say "you've drunk a fair bit by now". The kidney/bladder framing is the user's explicit ask — it's OK to mention, just don't say it the same way twice in a row.

### Generic

If multiple sedentary labels are present, pick the one that fits best or blend (e.g. eyes + wrists both deserving a break). Tables are starting points, not scripts — write your own sentence each turn.

## What to write (HW marker — fires async, no tool turn)

Embed at the start of your spoken reply:

```
[HW:/wellbeing/log:{"action":"nudge_hydration","notes":"<your nudge text>","user":"<current_user>"}] <your nudge sentence>
```

Same shape for break (`action="nudge_break"`) and toilet (`action="nudge_toilet"`). The marker:
- Is parsed and stripped by the runtime before TTS speaks the rest of your reply.
- Fires the POST asynchronously in the background; you do NOT wait for the result and there is NO tool turn here.
- Acts as the next reset point for that timer (timeline + delta + drinks-since-toilet computation).

**Posture variant — different endpoint.** Posture nudges target
`/posture/log` (NOT wellbeing log) with `action=nudge_posture` and
`nudge_level: 4`. Praise uses the same path with
`action=praise_posture`. Full marker shape + curl fallback in
`reference/posture.md`.

Skip the marker entirely when you took the **Reaction** path or stayed silent (`NO_REPLY`). The wellbeing marker is for `nudge_hydration` / `nudge_break` / `nudge_toilet` only — drink/break rows are already logged by LeLamp upstream. The posture marker is for `nudge_posture` / `praise_posture` only. The `notes` field is the same sentence you're about to speak — it's what the timeline will display.

**Do NOT use `curl` exec for this log.** That would consume a tool turn (~5-7s LLM-think on the result) for a side-effect that has nothing to wait for. The HW marker path is single-trip.

### Fallback (only if HW marker is rejected by the runtime)

If you see a runtime error parsing `[HW:/wellbeing/log:...]`, fall back to:

```bash
curl -s -X POST http://127.0.0.1:5000/api/wellbeing/log \
  -H 'Content-Type: application/json' \
  -d '{"action":"nudge_hydration","notes":"<your nudge text>","user":"<current_user>"}'
```

Posture fallback uses a different path — see `reference/posture.md`.

## On `presence.enter` / `presence.leave` / `presence.away`

Backend writes the `enter` / `leave` rows. You do nothing for these events — stay silent (`NO_REPLY`) unless there's something genuinely worth saying.

## Action value reference

| Action | Written by | Meaning |
|---|---|---|
| `drink`, `break` | LeLamp (on `motion.activity`, before event reaches you) | User acted. **Reset point.** |
| Raw eat labels — `eating burger`, `eating cake`, `eating carrots`, `eating chips`, `eating doughnuts`, `eating hotdog`, `eating ice cream`, `eating spaghetti`, `eating watermelon`, `dining`, `tasting food` | LeLamp (on `motion.activity`) | User ate. Raw labels kept (same hybrid as sedentary) so reaction phrasing can ground in the specific food. **Counts as meal signal** for the meal-reminder gate, **not** as a reset for break/hydration timers. |
| `using computer`, `writing`, `texting`, `reading book`, `reading newspaper`, `drawing`, `playing controller` | LeLamp (on `motion.activity`) | Sedentary — logged for timeline + phrasing. **Not a reset point.** |
| `enter`, `leave` | Backend (on `presence.*` events) | Session boundary; deduped against last presence row, so stranger-ID churn collapses. **Reset point.** |
| `nudge_hydration`, `nudge_break` | **You**, after speaking a nudge | Wellbeing log. Timeline + reset for next window. |
| `nudge_toilet` | **You**, after a toilet nudge | Wellbeing log. Resets `drinks_since_toilet_nudge` counter to 0. Sparse by design: only re-fires after another N drinks. |
| `morning_greeting` | **You**, on the morning-greeting route | Wellbeing log. Once-per-day gate; suppresses re-firing today. |
| `sleep_winddown` | **You**, on the sleep-winddown route | Wellbeing log. Once-per-day gate; suppresses re-firing tonight. |
| `meal_reminder` | **You**, on the meal-reminder route | Wellbeing log. Once-per-window gate (lunch / dinner separately). |
| `nudge_posture` | **You**, on the posture-nudge route | **Posture log** (separate JSONL). Carries `nudge_level=4`. LeLamp's cooldown already gates re-firing for ~30 min — your POST does not by itself reset it. |
| `praise_posture` | **You**, when the user clearly fixed posture after a recent nudge | **Posture log**. Rare — only when context shows improvement following an agent nudge in the last ~30 min. |

Emotional labels (`laughing`, `crying`, `yawning`, `singing`) are filtered upstream and never reach this skill via `motion.activity` — they'll arrive on a separate `motion.emotional` event in a future version.
