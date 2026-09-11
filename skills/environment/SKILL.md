---
name: environment
description: Interpret environmental sensor readings and sustained changes for room comfort and air quality. Use for [environment:update] events, questions about room temperature, humidity or air quality, and checking changes after ventilation or air cleaning. Requires the environment capability. Link to wellbeing for considerate proactive advice; do not diagnose health conditions or infer CO2 or oxygen from other measurements.
---

# Environment

## Scope and data

Work from the device's declared `environment` capability and the measurements actually present. Do not require a camera, microphone, emotion marker, or a particular sensor model. If the capability is absent, explain that this device has no available environment sensing; do not enable hardware or edit its declaration.

For a direct question or a requested current comparison, read the OS environment status API once:

```bash
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:5000/api/environment/status
```

This read-only route admits local device callers and checks the environment capability. It returns the OS envelope `{"status":1,"data":{...HAL snapshot...},"message":null}`; inspect `data`. A non-2xx response or `status:0` means unavailable. Do not bypass a denial through HAL or fetch credentials for the browser's authenticated hardware proxy.

Use the existing snapshot in an automatic event unless it is stale or the user explicitly requests a fresh check. Do not run a polling loop, start a cron job, invent a history endpoint, or promise a timed follow-up: OS owns polling, thresholds, sustained-change detection and cooldowns.

A usable snapshot has `enabled: true`, `state: "ready"`, `stale: false`, and a non-null `sample`. Check `age_s` and `sample.timestamp` before describing it as current. Disabled, starting, stopped, error, missing or stale data is unavailable, not zero pollution. Say so briefly on a direct question; stay silent on an unusable automatic event. Individual null or absent measurements are unknown: retain the other valid readings.

| Sample field | Meaning |
|---|---|
| `pm1_0_ug_m3`, `pm2_5_ug_m3`, `pm4_0_ug_m3`, `pm10_ug_m3` | Particle mass concentrations, µg/m³ |
| `temperature_c` | Temperature near the installed sensor, °C |
| `humidity_pct` | Relative humidity, % |
| `voc_index`, `nox_index` | Relative gas indices, not gas concentrations |

SEN55 supplies these fields, not CO₂, CO or O₂. Never infer oxygen shortage, CO₂ buildup, impaired cognition, a gas leak, a fire, or a medical condition from them. A future sensor can provide other fields; interpret them only with a documented measurement and unit.

VOC Index adapts to recent history (roughly 24 hours, baseline 100); NOx Index uses a different baseline (1). Neither baseline certifies safe air. Do not identify a chemical, smell or pollution source from either index. Describe an increase relative to previous readings. Do not convert indices into ppm or apply concentration guidelines to them. Instantaneous PM readings cannot establish compliance with 24-hour or annual WHO exposure guidelines. Sensor temperature may be affected by the enclosure and nearby electronics; do not present it as body temperature or a guaranteed room-wide measurement.

## Event contract

`[environment:update]` is followed by an OS-generated JSON object containing
`observed_at` (Unix seconds), `sample` (measurements and timestamp), `changes`
(keyed by metric, each with `previous`, `previous_at`, `current`, `delta`), and `sustained_s`.
Use the numeric change facts rather than guessing a trend from one raw sample.
Do not assume the event has the full status envelope or all metrics. A delayed
event describes its observation time, not necessarily the current room; obtain
current status before giving time-sensitive advice. `previous` is the detector's
previous acknowledged baseline and `previous_at` is its Unix timestamp;
`current` is the latest sample, not a rolling
average. `sustained_s` describes consecutive qualifying readings, not a health
exposure assessment. Missing context does not
justify invented thresholds, durations, health classifications or user activity.

## Choose the response

1. **Direct room-status question:** report the most relevant available measurements and an evidenced trend, usually in one or two sentences. Give numbers when useful or requested. A single snapshot cannot establish a trend or that the whole room is safe.
2. **Automatic `[environment:update]`:** use the supplied change facts. A significant change means OS's configurable change policy fired; it is not automatically a harmful level. Consider whether the change merits action using the environmental-care section of `skills/wellbeing/SKILL.md`. If there is no useful new advice, output exactly `NO_REPLY`.
3. **After an action:** compare a fresh value with an actual earlier, timestamped value in the event or conversation. State the observed direction without claiming causation: “Particles are lower than before.” If there is no comparison point, say that a change cannot yet be established. A later significant-change event may support an update, but no event is a guarantee of scheduled follow-up.

Do not route environmental updates to sensing's camera/presence reaction matrix or guard alerts, even while guard mode is active. No mandatory emotion, servo, light, camera or speech action. Never greet a person or infer presence from environmental data. Process only this event; it does not authorize resuming an unrelated task.

## Useful, proportionate advice

- Increased particles: suggest checking an identifiable source or using an available particle filter. Do not assume cooking, smoking, occupancy or a fire without supporting context.
- Gas-index changes: suggest checking recent activities or sources; ventilation may help when outdoor conditions allow. A HEPA particle filter does not remove every gas.
- Temperature or humidity: offer a modest comfort adjustment when supported by readings and user preferences. Do not diagnose dehydration, illness, sleep quality or productivity from the sensor.
- Do not blindly recommend opening a window when outside pollution or weather is unknown. Phrase that condition explicitly when relevant.
- Recommend actions; operate a fan, purifier, humidifier or HVAC only through an available integration under the user's authorization. Do not invent actuator APIs or promise devices that are not connected.
- Respect requests to stop reminders. Do not change detection policy or disable the sensor merely because a reading is inconvenient.

## Coordination with wellbeing

Read `skills/wellbeing/SKILL.md`'s **Environmental care** section for proactive phrasing and timing. Environment owns measurement interpretation; wellbeing contributes the user's preferences and available activity context. This is a one-way consultation of that section, not a handoff back through its activity router; do not bounce between the two skills. No activity logs, camera observations, identity or hydration/break counters are required to answer a room question. Do not fabricate a wellbeing context block or log an unsupported `nudge_environment` action.

When speaking proactively, offer one useful observation and at most one action in the user's language. Keep analysis and skill names out of the spoken reply. Silence is the literal `NO_REPLY`, without an explanation or hardware markers.


## Example decisions

| Input | Response direction |
|---|---|
| “How is the room?”; temperature valid, VOC null | Report temperature and other valid readings; VOC is unavailable. Do not turn null into zero. |
| “Why am I tired?” | Do not attribute fatigue to the sensor readings. If a room check is requested, describe only supported environmental facts. |
| VOC Index returns to 100 | Describe the relative change if relevant; never say pollution has cleared or air is safe. |
| Automatic change while context says the user is asleep or wants quiet | `NO_REPLY`; do not wake them, trigger guard mode or emit an emotion marker. |
| “I turned the purifier on; is it helping?” | Read current status and compare with a real earlier value if available. Without one, give the current reading and explain the missing comparison. Do not promise a timed recheck. |
| Delayed event or stale status | Do not describe old measurements as current. A failed fresh check means current conditions are unknown. |
