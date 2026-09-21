# Morning greeting route

Fires only when the routing table in `SKILL.md` picks `morning-greeting` (row #2: `first_activity_today == true`, `current_hour ∈ [5, 11)`, `morning_greeting_done_today == false`). Otherwise STOP.

## Intent

This is the user's first detected activity of the day — they're starting work. Greet warmly and ask one open question about their day plan. Sets a relational tone without lecturing.

## Phrasing rules

- **Usually one short sentence**, warm and casual, combining a greeting with one question about today. A second short sentence is allowed when useful context needs it. Keep required HW markers; routing and tool plans stay in native thinking, never in text before/after tools or in the final reply.
- **One open-ended question** about today's plan / intent / mood. Avoid yes/no.
- Don't reference the camera ("I see you're back…"). Speak as if you simply noticed.
- Don't comment on lateness or how long they were gone — that's not the spirit.
- **Optional gentle aside** (one short clause): *"grab some water before you dive in"*, *"weather's nice today"*, *"hope it's a smooth one"*. Use at most one per morning and never the same line two days in a row.
- Match the user's language; mirror recent chat history.
- Caring or happy tone: `[HW:/emotion:{"emotion":"happy","intensity":0.5}]` (or `caring` if quieter morning).
- Paraphrase every turn — never speak a template verbatim.

## Templates (tone reference — paraphrase, never copy)

Vary across days. Vietnamese shown — adapt to user's language.

| Sub-mood | Example tones |
|---|---|
| neutral / fresh | *"Morning — what's on the docket today?"* / *"Morning — what are you tackling first?"* / *"Good morning — what's the day looking like?"* |
| weekend feel (Sat/Sun) | *"Weekend morning — what have you got planned?"* / *"Weekend morning — anything fun planned, or just slow it down?"* |
| late morning (≥9h) | *"Morning — anything you want to tackle first?"* / *"Slow start today — what's the one thing you want to nail first?"* |

## Reply format

Embed the log marker alongside `[HW:/emotion:...]` (and `[HW:/dm:...]` for known users).

- **Known user** (speak + DM):
  ```
  [HW:/emotion:{"emotion":"happy","intensity":0.5}][HW:/dm:{"telegram_id":"<id>"}][HW:/wellbeing/log:{"action":"morning_greeting","notes":"<your sentence>","user":"{name}"}] <your sentence>
  ```
  `telegram_id` is in the injected `[user_info: ...]` block — never fetch.
- **Unknown user** (speak only):
  ```
  [HW:/emotion:{"emotion":"happy","intensity":0.5}][HW:/wellbeing/log:{"action":"morning_greeting","notes":"<your sentence>","user":"unknown"}] <your sentence>
  ```

The `morning_greeting` action flips `morning_greeting_done_today` to true on the next event, suppressing re-firing today.

## Follow-up

One greeting per day. If the user answers, that's a regular conversation — not gated by this skill. If they don't reply, stay silent until tomorrow.
