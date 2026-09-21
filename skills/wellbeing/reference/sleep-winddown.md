# Sleep wind-down route

Fires only when the routing table in `SKILL.md` picks `sleep-winddown` (row #3: `current_hour >= 21`, sedentary and/or `yawning` labels (no `drink`/`break`), `sleep_winddown_done_today == false`). Otherwise STOP.

> A `yawning` label in the message confirms the route and outranks the yawn reaction (#1b), which is capped at `current_hour < 21` precisely so the evening yawn lands here instead. You **may** name the yawn — *"that yawn was loud"* — but it does **not** change the register: stay soft either way, per the phrasing rules below. Naming the yawn is fine; naming the camera is not.

## Intent

Late evening: instead of pushing a break nudge (which implies "get back to work after"), gently suggest **winding down for sleep**. Don't moralize, don't say "you should sleep" — just plant the seed.

## Phrasing rules

- **Usually one short sentence**, soft and low-energy, acknowledging the late hour and inviting the user to wind down. A second short sentence is allowed when useful context needs it; after 23h prefer one short, quiet line. Keep required HW markers; routing and tool plans stay in native thinking, never in text before/after tools or in the final reply.
- Acknowledge the late hour without scolding.
- **No work-related ask.** Don't suggest stretching to keep going. The point is "wrap up", not "reset".
- **Optional health/comfort aside** (one short clause): *"or tomorrow morning's going to bite"*, *"give your eyes a rest"*, *"so you wake up actually rested"*. Use at most one per night and never the same line two nights in a row.
- Don't reference the camera or detection.
- Caring tone: `[HW:/emotion:{"emotion":"caring","intensity":0.4}]` (lower intensity than mid-day — quieter).
- Match the user's language.
- Paraphrase every turn — never speak a template verbatim.

## Templates (tone reference — paraphrase, never copy)

Vary across nights. Vietnamese shown — adapt to user's language.

| Hour | Example tones |
|---|---|
| 21–22h | *"Getting late — maybe wrap things up soon?"* / *"Late already — ready to wind down?"* |
| 22–23h | *"Nearly eleven — ready to leave the rest for tomorrow?"* / *"Closing in on bedtime — tomorrow's still there for it."* |
| ≥23h | *"Really late now — call it."* / *"It's really late — time to call it."* |

## Reply format

Embed the log marker alongside `[HW:/emotion:...]`.

- **Known user** (speak + DM):
  ```
  [HW:/emotion:{"emotion":"caring","intensity":0.4}][HW:/dm:{"telegram_id":"<id>"}][HW:/wellbeing/log:{"action":"sleep_winddown","notes":"<your sentence>","user":"{name}"}] <your sentence>
  ```
  `telegram_id` is in the injected `[user_info: ...]` block — never fetch.
- **Unknown user** (speak only):
  ```
  [HW:/emotion:{"emotion":"caring","intensity":0.4}][HW:/wellbeing/log:{"action":"sleep_winddown","notes":"<your sentence>","user":"unknown"}] <your sentence>
  ```

The `sleep_winddown` action flips `sleep_winddown_done_today` to true on the next event, suppressing re-firing tonight.

## Follow-up

One wind-down per night. After firing, defer to silence for the rest of the evening — don't keep nudging.
