# User-reported discomfort with optional room context

Use for the user's own report of fatigue, headache, dizziness, stuffiness or
difficulty concentrating. This is conversational support, not a diagnosis or
an activity detection. Do not apply it just because an environmental event
arrives, or tell someone they feel unwell based on sensor values.

## Respond to the person

Recognize the concern in the user's language. For ordinary discomfort, offer
one manageable next step and, when it would change the response, one concise
question such as when the headache started or whether it is unusually severe.
Do not turn a casual complaint into a compulsory medical questionnaire or
recite emergency warnings without relevant symptoms/context.

For headache without concerning context, a short screen break or a drink of
water can be offered without asserting dehydration or prescribing a cure.
Suggest medical advice if symptoms persist, worsen or recur. Do not introduce
medication advice through this environmental workflow.
[NHS: headaches](https://www.nhs.uk/symptoms/headaches/).

If the user already reports a sudden extremely severe headache, weakness,
speech/vision changes, confusion, seizure, or fever with a stiff neck, prioritize
urgent medical help. Do not wait for environment data or recommend ventilation
as the solution. Use local emergency services, not a UK phone number copied
from the source when the user's location is elsewhere or unknown.
[NHS: when headaches need urgent help](https://www.nhs.uk/symptoms/headaches/).

Reported combustion exposure, a CO alarm, or several people becoming unwell
in the same room can warrant suspected-exposure advice. These sensors do not
measure CO and cannot exclude it. Advise getting outside and urgent medical
advice; do not suggest staying inside to adjust a window, investigate an
appliance or wait for another reading. Breathing difficulty, collapse,
confusion or chest pain warrants emergency help. Do not introduce suspected
CO poisoning from a headache alone or from CO₂ readings.
[NHS: carbon monoxide poisoning](https://www.nhs.uk/conditions/carbon-monoxide-poisoning/).

## Add environment only when it is available and useful

- If `environment` is not explicitly declared (including unknown capability),
  skip this step. Wellbeing remains usable without the environment skill.
- If declared, use `skills/environment/SKILL.md` for one bounded status read
  or a supplied current status snapshot. Apply its data rules and return to
  this response. If that skill or its data was already consulted for this
  turn, reuse it: do not reload skills, repeat the read or recurse through
  either activity router.
- If the API fails, the sample is all null/stale, or a relevant metric is
  absent, silently omit that environmental explanation. No retries, fallback
  HAL requests, hardware setup advice or “my sensor is unavailable” preamble.
  Answer an explicit question about missing readings honestly if asked.
- Use only relevant, fresh measurements. No CO₂ means no CO₂ claim, even when
  VOC is available. Other valid metrics can still support their own observation.
  A user's word “stuffy” alone is not a measured CO₂ value.
- Keep the report and evidence separate: “CO₂ is 1,800 ppm right now” can be
  followed by a conditional ventilation suggestion, but not “that's why your
  head hurts”. A lower reading does not dismiss their symptoms.
- Give at most one comfort suggestion at a time; choose between a general
  comfort step and an environmental action rather than stacking both lists.
  If the user cannot or does not want to open a window, respect that and offer
  another practical option only if useful. Urgent help takes precedence over
  this brevity preference.

For ventilation choices and the distinction between rising and consistently
high CO₂, use the environment skill's **Discomfort and ventilation context**
section. Do not change OS thresholds, start polling, or use occupational
exposure limits to declare a home safe.

## Continue without overpromising

On a later user request or actual environment event, compare fresh values with
an earlier timestamped value already available. A decrease does not prove the
action caused it or that the person's symptoms resolved. If they still feel
unwell, respond to that concern even if the numbers improved. Do not promise
a timed check, start a schedule, operate appliances, or post wellbeing
activity/nudge logs on the strength of this conversation alone. Respect requests
for quiet; direct requests for help still receive a response.

## Example decisions

Examples are response directions, not fixed scripts; never invent the supplied
readings or user context.

| Input and available evidence | Response direction |
|---|---|
| “Mình mệt và nhức đầu quá”; no environment capability | Respond warmly, suggest one ordinary comfort step and optionally ask about onset/severity. No sensor mention or environmental tool call. |
| Same complaint; declared capability but status fails or all values null | Same ordinary support. Do not wait, retry, or tell the user to enable sensors. |
| Same complaint; fresh CO₂ 1,800 ppm, no earlier reading | Acknowledge discomfort, report the value if useful, suggest ventilation conditionally. No claim of a rise, poisoning or symptom cause. |
| Automatic CO₂ change 500 → 720 ppm, no symptoms or useful action context | Do not call the room dangerous or manufacture a discomfort conversation; `NO_REPLY` can be appropriate. |
| CO₂ rose; outdoor smoke is reported | Do not recommend opening a window into smoke. Consider an available fresh-air system with appropriate filtration or another suitable space. |
| CO₂ missing; valid VOC index | No estimate of CO₂ and no CO₂-based advice. Use any relevant valid data without making up a cause. |
| Headache with sudden weakness; CO₂ 650 ppm | Urgent help first; no sensor check or reassurance from the CO₂ value. |
| Several people feel ill near a fuel-burning heater | Treat reported exposure as the concern; leave for outside air and seek help, without waiting for these sensors. |
| “Still feeling bad”; CO₂ fell after opening a window | Do not equate better numbers with recovery. Address persistent symptoms; no automatic congratulation. |
| “Don't keep reminding me”; another ordinary environment event | Respect quiet preferences. Do not create a new reminder or repeat the suggestion. |

Source guidance reviewed 2026-09-14. These are routing and response instructions,
not a validated clinical triage system or a substitute for a CO alarm.
