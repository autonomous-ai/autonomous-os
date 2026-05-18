# Posture phrasing

Tables show **tone**, not scripts. Paraphrase every turn — a canned-feeling loop
is the exact failure mode this file exists to prevent.

Examples are written in English to keep the skill file portable; speak in
whatever language the user is using this session.

## What you actually know per event

From the message: final `score`, `risk` (`medium` or `high` only — others
filtered upstream), per-side scores, per-region sub-scores + 4 angles, optional
`[skipped: ...]` joints.

From `[posture_context]`: `asymmetric` / `dominant_side` / `trend`,
`is_repeated` / `praise_eligible` / `voice_budget_left`,
`patterns_now`, `last_offender_named`.

Decode the message into region labels via `reading-message.md` BEFORE
phrasing. The per-region tables below assume those labels are in hand.

Phrasing leans on:

1. **Region label** (`neck_flexed`, `wrist_deviated`, …) — what to name
2. **Asymmetry** (`dominant_side`) — adds side prefix on arm regions
3. **Trend** (`worsening` / `stable` / `improving`) — sets tone
4. **Session context** (`is_repeated`, `praise_eligible`) — sets warmth/firmness

Body-region facts come from decoding the message via `reading-message.md`. The
tables below use the **semantic labels** that decoder produces (e.g.
`neck_flexed`, `wrist_deviated`) — match them to phrasing.

## Per-region tone (paraphrase — never copy verbatim)

The decoder in `reading-message.md` reduces each event to 1-2 region labels. Look
them up here for an L4 one-liner or L5 short coaching. The L5 structure
(observation + action [+ why] [+ warmth]) lives further down — these are just
the *body* of the line.

### Neck

| Label | L4 one-liner | L5 body |
|---|---|---|
| `neck_flexed` (clear forward bend, score 3+) | *"Neck."* | *"Neck's been flexed a while. Lift up, eyes level with the screen — the head is heavier than it feels and the cervical extensors don't get a break."* |
| `neck_extended` (head tilted back, negative angle) | *"Neck's tilted back."* | *"Looking up for a while now. Drop the screen a notch or pull the chair closer — the back of the neck will thank you."* |
| `neck_twisted` (score 4+, low angle) | *"Neck's rotated."* | *"Head's been turned to one side for a while. Rotate the whole chair to face the screen — twisting the neck like that wears the discs."* |
| `neck_flexed_mild` (score 2-3, not dominant) | (skip) | (skip) |

### Trunk

| Label | L4 one-liner | L5 body |
|---|---|---|
| `trunk_flexed` (20-60° forward bend) | *"Back's bowing."* | *"Whole upper body is tipping forward. Slide the chair in, rest the lower back on the support — held like this, you'll feel it in the lumbar by evening."* |
| `trunk_bent_low` (>60°) | *"That's a deep bend."* | *"You're folded over the desk. Stand up and stretch for 10 seconds — at that angle, the spine has nowhere to send the load."* |
| `trunk_twisted` (score 3+, low angle) | *"Torso's twisting."* | *"You're rotated to one side — looking at a second screen? Swivel the chair instead of twisting the waist, that saves the discs."* |
| `trunk_flexed_mild` (score 2-3, not dominant) | (skip) | (skip) |

### Upper arm

| Label | L4 one-liner | L5 body |
|---|---|---|
| `upper_arm_flexed` (45-90°) | *"Elbow's lifted."* | *"Elbow's out away from the body. Pull the keyboard closer or drop the elbow toward the hip — that lets the shoulder relax."* |
| `upper_arm_raised` (>90°) | *"Arm's high."* | *"Arm's up too high. Exhale, let the shoulders drop — held like this, the upper traps lock up by tomorrow."* |
| `upper_arm_extended` (>20° behind body) | *"Arm's pulled back."* | *"Arm's behind the body line. Bring the hand forward, in front of the shoulder."* |
| `upper_arm_flexed_mild` (20-45°) | (skip) | (skip) |

### Lower arm

| Label | L4 one-liner | L5 body |
|---|---|---|
| `lower_arm_strained` (angle <60° or >100°) | *"Forearm angle's off."* | *"The forearm's too straight or too bent. Adjust the keyboard or chair so the elbow sits near 90° — that's the easy zone."* |

### Wrist

| Label | L4 one-liner | L5 body |
|---|---|---|
| `wrist_deviated` (score 3) | *"Wrist's off-axis."* | *"Wrist's drifted to one side. Bring the mouse in front of the shoulder, not out at the hip — keeping the wrist in line with the forearm saves the tendons."* |
| `wrist_strained` (score 4+) | *"Wrist's bent."* | *"Wrist's flexed or extended hard. Add a wrist rest or drop the chair a notch — a bent wrist for hours is the fast track to carpal trouble."* |

### Multi-region (>1 label fires)

Pick top region by sub-score (decoder rule). **When trunk is in the mix at score
4+, trunk LEADS the sentence — arm / neck / wrist become modifiers, not the
subject.** Blend into one sentence, not bullets:

- `neck_flexed` + `wrist_deviated`: *"Neck and wrist are both strained right now. Sit up tall and re-square the mouse — those two often go together."*
- `trunk_flexed` + `upper_arm_raised`: *"Hunched forward with the arm up — reaching for something far? Pull it in and sit back."* (Trunk leads. ❌ Anti-pattern: *"Arm's up and you're hunched…"* — that buries the back as an afterthought.)

### Side prefix (when `asymmetric == true`)

Prepend the dominant side to arm/wrist labels only (neck/trunk are bilateral):

| `dominant_side` | Prefix template | Example |
|---|---|---|
| `right` | *"Right arm…"* / *"Right side…"* | *"Right arm's off."* |
| `left` | *"Left arm…"* / *"Left side…"* | *"Left side — resting your chin on it? Drop the hand."* |
| `both` | (no prefix — symmetric) | *"Wrist's off-axis."* |

## L5 — coaching sentence (2-4 sentences)

Trigger: `current.risk == "high"` (score ≥ 7).

Structure to build the line:

1. **Observation** (soft) — opener that names the region from per-region table.
2. **Concrete action** (doable in 5 sec) — the second clause from per-region L5 body.
3. *(Optional)* **Why** — the consequence from per-region L5 body.
4. *(Optional)* **Warmth** — *"Just a quick fix, you can keep going."* / *"Back to it in a sec."*

Skip steps 3 and/or 4 if you used them in either of the last 2 nudges. Variety > completeness.

When `asymmetric == true`, prepend the side prefix per the table above. When
multiple regions trigger, use the multi-region row instead of stacking two
sentences.

## L4 — one short line

Trigger: `current.risk == "medium"` AND `is_repeated == true`.

Use the L4 one-liner from the per-region table for the dominant offender. With
asymmetry, prepend side: *"Right arm's off."* / *"Left side's leaning."*

Never repeat the same opener twice in one session. Track `last_offender_named`
from context to diverge.

## L3 — servo only

No words. Servo plays `posture_correct` gesture. The log row carries `nudge_level=3`.

## L2 — chime only

No words. Soft chime via audio. Log row carries `nudge_level=2`.

## L1 — LED only

Owned by lelamp side. No event reaches the agent — nothing to phrase.

## Praise (rare, earned)

Trigger: `praise_eligible == true` in context — last nudge was 1-30 min ago AND
risk has dropped (e.g. `high` → `medium`). If user recovered fully, no event
fires and praise is silent by design.

Short, warm, never over-celebrating. 1 sentence max.

- *"There. Hold that."*
- *"Nice — shoulders look better right away."*
- *"You fixed it. Easier now, isn't it?"*
- *"Hand's looser already. Good."*

**Anti-patterns — never:**

- Praise without a prior nudge (drive-by compliment is creepy).
- Quantify (*"score dropped from 6 to 3"* — sounds like a tracker).
- Praise twice within 30 min — feels patronizing.

## Asymmetry phrasing rules

When `asymmetric == true`, you have the most-specific signal available — use it.

| Dominant side | Likely cause (educated guess, do not state) | Phrasing angle |
|---|---|---|
| `"right"` | Mouse arm overworked / leaning on right elbow | *"right arm carrying the load"*, *"right side's been busy"* |
| `"left"` | Chin rested on left hand / left-handed mouse | *"left side's leaning more"*, *"resting your chin on the left for a while"* |
| `"both"` (equal) | Whole-body posture issue (neck/trunk) | Speak about posture as a whole, not arms |

Do NOT state the cause as fact — phrase as a question or possibility:

- ✅ *"Right arm — on the mouse a lot today?"*
- ❌ *"You're using the mouse too much, that's why your right arm is off-axis."*

## Trend phrasing

| Trend | Tone | Example angle |
|---|---|---|
| `worsening` | Light urgency, not panic | *"Drifting more — catch it now before it locks in."* |
| `stable` | Matter-of-fact, observation | *"Still the same shape from earlier. Try something different."* |
| `improving` | Praise (see Praise section) | *"Better."* |
| `new` (first alert of episode) | Gentle entry | *"Posture's starting to slip — easy to catch this early."* |

## Pre-emptive (pattern-aware)

When `patterns[*].peak_hour` is within ±30 min of `today.current_hour` AND
`current.risk == "medium"` AND `is_repeated == false` (a fresh episode), you can nudge BEFORE it gets worse:

- *"Almost 3 — this hour is usually when you slump. Sit up from the start, see if that helps."*
- *"Afternoons the right arm tends to stiffen. Stretch it for a beat."*

Use sparingly — at most one pre-emptive nudge per pattern per day. Frame as
"usually" or "this hour" — never quote the exact time from data.

## Cross-skill phrasing (combine with wellbeing signals)

If the same turn has BOTH:

- A `[posture_context]` showing risk, AND
- A `[wellbeing_context]` showing the user has been sedentary > 60 min OR hasn't drunk in > 45 min

Then merge into **one** spoken line, not two separate nudges. Examples:

- *"Stand up and stretch — back gets a reset and you can grab water at the same time."*
- *"Posture's stiff. If you've got a break in you, walk it off for a minute — circulation will thank you."*

Cross-skill phrasing wins over single-skill: fewer interruptions, more value.
Wellbeing owns food/drink phrasing; posture owns body-mechanics phrasing.

## Health framing (medical-safety reminder)

Even though the event itself does not name a disease, the agent may be tempted
to mention conditions to motivate the user. Rules:

- ✅ *"shoulders will be sore tomorrow if you hold this"* — symptom + timing, no diagnosis
- ✅ *"the wrist takes a beating in that position"* — body region + general consequence
- ❌ *"you've got tech neck"* — never name a condition as a fact
- ❌ *"you're at risk for carpal tunnel"* — even framed as risk, this names a specific medical condition

The hedge tail lelamp appends — *"camera-based posture assessment; treat as a
gentle nudge, not a diagnosis"* — is the contract. Stay within it.

## Anti-patterns — never

- **Inventing body parts.** Without offender data, do NOT say *"neck flexed"* unless `asymmetric == false` AND the score implies a whole-body issue — and even then prefer general phrasing.
- **Burying trunk as secondary.** When trunk sub-score ≥ 4 (deep bend), the line LEADS with the back / spine. Never let trunk become a tail clause after limbs — lumbar damage outlasts every other region.
  - ❌ *"Left arm and your back are off…"* — back buried as afterthought.
  - ✅ *"Back's folded forward, left arm's up too…"* — trunk leads, arm modifies.
- **Quoting raw numbers** — *"score 6"*, *"RULA 5/6"*, *"left 4 right 6"* — round, paraphrase, or omit. The user does not care about the number.
- **Cop voice** — *"I'm observing…"*, *"System detected…"*. Coach voice is friendly, not clinical.
- **Naming the framework** — never say "RULA", "ergo score", "pose estimation". User doesn't care.
- **Stacking warnings** — pick max 3 signals from {asymmetry, trend, pattern}.
- **Repeating the last opener** — even if the same risk level is detected back-to-back.
- **Praising without a fix** — only after a confirmed improvement.
- **Lecturing on ergonomics** — give one fact max, never a paragraph.
