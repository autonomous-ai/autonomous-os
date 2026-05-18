# Posture nudge

Triggered by decision row #5: `[posture_summary]` block present in the
`motion.activity` message. One spoken nudge per cooldown window
(~30 min), then silent — the cooldown is enforced upstream on lelamp,
so a second summary won't arrive for ~30 min anyway.

## When the block appears

LeLamp folds two extra blocks into `motion.activity` ONLY when ALL hold:

- Sedentary streak ≥ `POSE_STREAK_MIN_GATE_S` (test 10 min / prod 30 min).
- Sample buffer is full (`POSE_WINDOW_SAMPLES` — test 10 / prod 30
  valid samples across that many minutes).
- `bad_ratio ≥ POSE_BAD_RATIO` (test 0.3 / prod 0.5).
- No previous summary was injected within `POSE_NUDGE_COOLDOWN_S`
  (test 10 min / prod 30 min).

If the block is absent: posture is fine, the streak is still short,
the buffer isn't full yet, or we're in cooldown — **do not nudge
posture this turn**, regardless of how the user feels in the moment.
The wellbeing
context's `last_posture_nudge_age_min` field gives the corroborating
agent-side view (-1 if no nudge today).

## Schema

```
[computer_streak_min: 47]
[posture_summary: {
  "bad_ratio": 0.73,            // fraction of samples ≥ medium risk
  "samples": 30,                // samples in the rolling window
  "bad_samples": 22,            // of those, how many were medium+
  "window_min": 30,             // rolling window length in minutes
  "region_frequency": {         // count of bad samples where each region was ≥ score 3
    "neck": 19, "upper_arm": 17, "trunk": 4, "lower_arm": 2, "wrist": 0
  },
  "dominant_region": "neck",    // top-frequency region
  "dominant_count": 19,
  "latest_score": 5,            // most-recent frame final RULA score
  "latest_risk_level": 3,       // 3=medium, 4=high (1/2 are filtered upstream)
  "latest_left":  { "score":…, "body_scores":{ "neck":…, "upper_arm":…, … }, … },
  "latest_right": { … },
  "streak_min": 47              // mirrors [computer_streak_min] for convenience
}]
```

A turn may carry the posture summary AND independent wellbeing deltas
(hydration / break thresholds). The decision table puts posture-nudge
ahead of break/hydration so we don't double up on "stand up" — the
posture line naturally tells them to move and resets the break timer
when they actually do.

## Anchor the line on

1. **Streak length** (`streak_min` / `[computer_streak_min]`) — round it
   (*"nearly an hour"*, *"well past an hour"*, *"almost two hours"*).
   **Never quote the exact number** — feels like a tracker.
2. **`dominant_region`** — the one body part that showed up bad most often.
3. **One concrete fix** — drop shoulders, tuck chin, sit back, …
4. *(Optional)* one short health-context clause — sore by tonight, neck
   holds the head's weight all day, lumbar disc, … Never name a disease
   as fact (see "Health framing" below).

## Tone (paraphrase — never copy)

| `dominant_region` | Example tone |
|---|---|
| `neck`       | *"You've been hunched into the screen for nearly an hour. Lift your chin, get the eyes level with the monitor — the back of the neck doesn't get a real break otherwise."* |
| `trunk`      | *"That's almost an hour folded over the desk. Sit back, let the chair take the lower-back load for a few minutes — it'll catch up with you tonight if you don't."* |
| `upper_arm`  | *"Shoulders have been creeping up for a while now. Exhale, drop them once, and pull the keyboard closer — held high like that, the traps lock up by tomorrow."* |
| `lower_arm`  | *"Forearms have been at a rough angle for a stretch. Adjust the keyboard so the elbows can sit near 90°, then shake out the hands."* |
| `wrist`      | *"Wrist's been bent under the mouse for a while. Reset its position so it's in line with the forearm — the tendons can take a beating in that pose."* |
| (empty / mixed) | *"Posture's been drifting for the past stretch. Stand up for thirty seconds, give the whole body a reset — easier than untangling each spot one by one."* |

## Side prefix (asymmetric)

When `latest_left` and `latest_right` arm sub-scores differ by ≥ 2,
prepend the side (e.g. *"right shoulder"*). Neck and trunk are bilateral
— never side them. Be careful: pose models can flip left/right when the
user is facing away — if the latest frame angles look suspicious
(multiple values near ±170°+), prefer a generic non-sided line.

## Praise route

Triggered when `last_posture_nudge_age_min ∈ [1, 30]` AND the new
summary shows clear improvement vs. the previous nudge (lower
`bad_ratio` than what plausibly triggered the last nudge, or
`latest_risk_level` dropped from 4 to 3). Speak ONE short warm line and
post `praise_posture` (same `/posture/log` endpoint).

Anti-patterns:
- Praise without a recent nudge (drive-by compliment is creepy).
- Quantify (*"score dropped from 6 to 3"* — sounds like a tracker).
- Praise more than once in any 30-min window.

## Health framing — guardrail

- ✅ *"the back of the neck doesn't get a real break"* — region +
  general consequence.
- ✅ *"lumbar will feel it tonight"* — symptom + timing, no diagnosis.
- ❌ *"you've got tech neck"* / *"that's carpal tunnel territory"* —
  never name a condition as a fact, even framed as "risk".

The hedge baked into LeLamp's contract — *"camera-based posture
assessment; treat as a gentle nudge, not a diagnosis"* — is what keeps
you within scope. Don't override it.

## HW marker

```
[HW:/posture/log:{"action":"nudge_posture","nudge_level":4,"notes":"<your nudge text>","user":"<current_user>"}] <your nudge sentence>
```

`praise_posture` uses the same path with `action="praise_posture"` and
omits `nudge_level`.

### Curl fallback (only if HW marker is rejected)

```bash
curl -s -X POST http://127.0.0.1:5000/api/posture/log \
  -H 'Content-Type: application/json' \
  -d '{"action":"nudge_posture","nudge_level":4,"notes":"<your nudge text>","user":"<current_user>"}'
```
