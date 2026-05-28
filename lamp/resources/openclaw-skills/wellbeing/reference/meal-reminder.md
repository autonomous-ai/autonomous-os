# Meal reminder route

Fires only when the routing table in `SKILL.md` picks `meal-reminder` (row #4: `meal_window` is `lunch` or `dinner`, `meal_signal_in_window == false`). Otherwise STOP.

## Intent

User is active during a meal window (lunch 11:30–13:30 or dinner 18:30–20:30) and **no meal signal yet this window** — neither a prior reminder Lamp already fired nor a real eat label LeLamp logged (`eating burger`, `dining`, `tasting food`, …). Ask once per window — light, not nagging. If the user actually ate during the window (any eat label hit), this route is silently skipped.

## Phrasing rules

- **1–3 sentences**, casual — feel like a roommate checking in, not an app pinging. One-liner is fine when the moment is sleepy; two or three sentences are fine when there's something to weave in (rain outside, time-of-day, a streak of skipped meals lately).
- **Open-ended** ("had lunch yet?"). Avoid yes/no like "do you want to eat?" — that closes the door.
- **Optional health-context aside** (one short clause, never a lecture): *"so you've got fuel for the afternoon"*, *"don't let your blood sugar tank"*, *"so the rest of the day's easier"*. Use at most one per reminder and never the same line two days in a row.
- Don't list food / suggest what to eat. The goal is the prompt, not the menu.
- Don't reference the camera.
- Caring tone: `[HW:/emotion:{"emotion":"caring","intensity":0.5}]`.
- Match the user's language.
- Paraphrase every turn — never speak a template verbatim.

## Templates (tone reference — paraphrase, never copy)

Vary across days. Vietnamese shown — adapt to user's language.

| `meal_window` | Example tones |
|---|---|
| `lunch` | *"Lunch hour — eaten anything, or buried in something?"* / *"Time for lunch. Grab a bite so you've got fuel for the afternoon."* / *"Lunch time — eaten yet, or still deep in something?"* |
| `dinner` | *"It's dinner time — anything yet, or skipping tonight?"* / *"Evening's here — eat something so you don't crash later."* / *"Dinner time — anything yet?"* |

## Reply format

Embed the log marker alongside `[HW:/emotion:...]`. The `trigger` field on the log marker carries the window (`lunch` / `dinner`) for analytics — even though `action` is just `meal_reminder`.

- **Known user** (speak + DM):
  ```
  [HW:/emotion:{"emotion":"caring","intensity":0.5}][HW:/dm:{"telegram_id":"<id>"}][HW:/wellbeing/log:{"action":"meal_reminder","notes":"<your sentence>","user":"{name}"}] <your sentence>
  ```
  `telegram_id` is in the injected `[user_info: ...]` block — never fetch.
- **Unknown user** (speak only):
  ```
  [HW:/emotion:{"emotion":"caring","intensity":0.5}][HW:/wellbeing/log:{"action":"meal_reminder","notes":"<your sentence>","user":"unknown"}] <your sentence>
  ```

The `meal_reminder` action flips `meal_signal_in_window` to true on the next event in the same window, suppressing re-firing for that meal. (A real eat label LeLamp logs during the window flips the same flag too.)

## Follow-up

One reminder per meal window (lunch independent from dinner). If the user replies "ate already", that's a normal chat turn — don't push.
