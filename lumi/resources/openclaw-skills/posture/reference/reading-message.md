# Reading the posture event message

How to decode the lelamp `pose.ergo_risk` message into the body-region facts the
phrasing tables expect. Follows the **RULA worksheet** (McAtamney & Corlett, 1993).

> Lelamp emits this event only when the final score is **≥ 5** (medium or high).
> Negligible / low postures never reach this skill — don't write rules for them here.

## Message format (literal)

```
Ergonomic risk detected: RULA score <N> (<risk> risk).
Left  (score=<X>, risk=<Y>): upper_arm=<a> (<°>°), lower_arm=<b> (<°>°), wrist=<c>, neck=<d> (<°>°), trunk=<e> (<°>°).
Right (score=<X>, risk=<Y>): upper_arm=<a> (<°>°), lower_arm=<b> (<°>°), wrist=<c>, neck=<d> (<°>°), trunk=<e> (<°>°).
(camera-based posture assessment; treat as a gentle nudge, not a diagnosis.)
```

- `<N>` — final RULA score (1-7+). Worse of left/right.
- `<risk>` — `medium` (5-6) or `high` (7+). `negligible` / `low` are filtered upstream.
- Each side reports the same 5 sub-scores + 4 angles. `legs` and `wrist_twist` are not exposed in this build.
- A `[skipped: <joints>]` tail appears when some joints were occluded or low-confidence — skip claims about those regions.

## Sub-score scale (RULA standard, identical per region)

| Sub-score | Meaning | When to mention |
|---|---|---|
| **1-2** | Acceptable for this region | Never mention this region |
| **3** | Mild concern | Mention only if it's the highest among regions |
| **4+** | Significant strain | This is the priority — speak to this region |

The final score is computed via RULA's lookup tables (Table A + Table B + Table C),
so sub-score 4 is roughly the threshold where the region drives the overall risk.

## Per-region decoder

### Upper arm (`upper_arm` + `upper_arm_angle`)

RULA Step 1. Angle = shoulder flexion (forward) / extension (backward).

| Sub-score | Angle range | Semantic label | Tone hint |
|---|---|---|---|
| 1 | −20° to 20° | neutral — don't mention | — |
| 2 | < −20° (extension) | `upper_arm_extended` | "arm tilted back behind the body" |
| 2-3 | 20-45° flexion | `upper_arm_flexed_mild` | "elbow lifted slightly" |
| 3-4 | 45-90° flexion | `upper_arm_flexed` | "elbow out / arm raised" |
| 4+ | > 90° flexion | `upper_arm_raised` | "shoulder / arm up high" |

Adjustments folded into the score (not separately exposed): shoulder raised (+1),
arm abducted (+1), arm supported (−1).

### Lower arm (`lower_arm` + `lower_arm_angle`)

RULA Step 2. Angle = elbow flexion (degree between upper and lower arm).

| Sub-score | Angle range | Semantic label | Tone hint |
|---|---|---|---|
| 1 | 60-100° | neutral — don't mention | — |
| 2-3 | < 60° (too straight) OR > 100° (over-bent) | `lower_arm_strained` | "forearm too straight / too bent" |

Score 3 typically appears when the arm is working across the midline or out to
the side of the body.

### Wrist (`wrist`, no angle)

RULA Step 3 + 4. Combines flexion/extension and ulnar/radial deviation. No angle
exposed — score alone is the signal.

| Sub-score | Semantic label | Tone hint |
|---|---|---|
| 1 | neutral — don't mention | — |
| 2 | mild — don't mention unless dominant | — |
| 3 | `wrist_deviated` | "cổ tay lệch / gập" |
| 4 | `wrist_strained` | "wrist bent under strain" |

### Neck (`neck` + `neck_angle`)

RULA Step 9. Signed flexion angle.

| Sub-score | Angle range | Semantic label | Tone hint |
|---|---|---|---|
| 1 | 0-10° | neutral — don't mention | — |
| 2 | 10-20° flexion | `neck_flexed_mild` | "head dipped a bit" — usually skip |
| 3 | > 20° flexion | `neck_flexed` | "head flexed forward" |
| 4 | extension (negative) | `neck_extended` | "cổ ngửa ra sau" |
| 3-4 + adjustment | twist / side-bending | `neck_twisted` | "neck rotated / tilted to side" — only when score >=4 and angle <20° (twist not captured in angle alone) |

> Sub-score 4 with `angle < 20°` strongly suggests twist or side-bending rather
> than pure flexion. Lean on twist phrasing in that case.

### Trunk (`trunk` + `trunk_angle`)

RULA Step 10. Forward bend angle.

| Sub-score | Angle range | Semantic label | Tone hint |
|---|---|---|---|
| 1 | well-supported, ~0° | neutral — don't mention | — |
| 2 | 0-20° flexion | `trunk_flexed_mild` | usually skip |
| 3 | 20-60° flexion | `trunk_flexed` | "bowed / hunched forward" |
| 4 | > 60° flexion | `trunk_bent_low` | "deep forward bend" |
| 3-4 + low angle | twisted or side bent | `trunk_twisted` | "torso rotated / leaning sideways" |

## Combining signals — which region to name

When multiple regions trigger:

1. **Highest sub-score wins.** If `neck=4, wrist=3, trunk=3` → name **neck**.
2. **Trunk at sub-score 4+ leads.** Deep trunk flexion (>60°) is the single biggest
   driver of chronic lumbar damage and pushes RULA Group B by itself. Whenever
   trunk hits 4+, it leads the sentence — even when tied with arm or neck.
3. **Ties otherwise: prefer the region not mentioned in your last 2 nudges this session.**
4. **Ties with no recency clue: prefer trunk > neck > arm > wrist.**
   Trunk and neck drive RULA Group B; trunk injuries are the slowest to heal.
   Wrist ranks last because no angle is exposed — lowest signal reliability.
5. **Never name more than 2 regions in one sentence.** Pick the top 1-2.

## Left vs right asymmetry

Left and right may have different sub-scores **only on arm regions** (upper_arm,
lower_arm, wrist). Neck and trunk are bilateral — left and right report the same
value for those.

| `|L_score − R_score|` | Treat as | Phrasing angle |
|---|---|---|
| `0-1` | Symmetric | Speak about posture as a whole. Don't mention sides. |
| `>= 2` | Asymmetric | Name the side. Look at which arm-region scores differ to find what's worse. |

Example: `Left wrist=2`, `Right wrist=4` → say *"right wrist"*, not generic *"wrist"*.

## Skipped joints (`[skipped: ...]`)

When the message includes `[skipped: left_wrist, left_elbow]`:

- **Do not** speak about the skipped joints — confidence is too low.
- **Do not** trust asymmetry comparisons involving skipped joints — drop to symmetric framing.
- If most joints on one side are skipped (≥3 out of 5), drop side-specific phrasing
  entirely and speak generically.

## What to NOT say

Anti-patterns that reading raw scores easily triggers — re-read these before speaking:

- ❌ *"Your neck is at 35 degrees."* — never quote raw angles.
- ❌ *"upper_arm score is 4."* — never quote sub-scores.
- ❌ *"Your RULA is 6."* — never name the framework.
- ❌ *"Left=5, right=6."* — never recite the side scores.
- ❌ Naming a region whose sub-score is < 3 — that region is OK.
- ❌ Naming a region listed in `[skipped: ...]` — data is unreliable.

The score → label mapping above translates numbers into the vocabulary the
phrasing tables in `phrasing.md` use. Once you have the label (vd `neck_flexed`),
look up the corresponding row in `phrasing.md` and paraphrase.

## Worked example

Message in:
```
Ergonomic risk detected: RULA score 6 (medium risk).
Left  (score=5, risk=medium): upper_arm=3 (55°), lower_arm=2 (75°), wrist=2, neck=4 (35°), trunk=2 (12°).
Right (score=6, risk=medium): upper_arm=4 (75°), lower_arm=2 (75°), wrist=3, neck=4 (35°), trunk=2 (12°).
(camera-based posture assessment; treat as a gentle nudge, not a diagnosis.)
```

Decoder steps:

1. **Asymmetric?** Left=5, Right=6 → `|diff|=1` → **symmetric** (treat as whole).
2. **Per region:**
   - `neck=4 (35°)` → `neck_flexed` (sub-score 4 + angle > 20°). ⭐ highest
   - `upper_arm=3-4 (55-75°)` → `upper_arm_flexed`. Secondary.
   - `wrist=2-3` → not significant (right side 3 only, asymmetric not strong enough).
   - `lower_arm=2, trunk=2` → don't mention.
3. **Pick top region:** neck (highest score, drives risk).
4. **Look up `phrasing.md`** for `neck_flexed`:
   - L4 line: *"Neck."*
   - L5 line: *"Neck's been flexed a while. Lift up, eyes level with the screen."*
5. **Speak.**
