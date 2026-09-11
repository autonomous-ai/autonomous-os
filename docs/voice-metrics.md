# Voice response metrics

One tracker on the device telemetry pipe — see [`docs/telemetry.md`](telemetry.md)
for the pipe itself, the on/off switch, and the rules every tracker follows.

Measurement only. Nothing in this document changes what the device says, when
it speaks, or when it stays quiet — it records what already happened so three
numbers can be computed from real devices:

| KPI | Question | Target |
|-----|----------|--------|
| **KPI-1** | Share of eligible voice interactions acknowledged within **3 s** of the detected end of the user's speech | ≥ 95 % |
| **KPI-2** | Share of eligible suppression boundaries where an **outdated** reply was actually played | < 5 % |
| **KPI-3** | Share of eligible voice task turns that finish executing | ≥ 85 % |

The KPI-1 and KPI-2 targets are **provisional**. Every raw duration is stored, so a threshold
can be re-decided from the data instead of by re-instrumenting devices.

## Where the code lives

| Layer | Path | Role |
|-------|------|------|
| HAL tracker | `hal/telemetry/voice_metrics.py` | Owns interaction ids, ack/stale detection, task cohort and realtime execution evidence. |
| OS task evidence | `system/telemetry/voice_task.go` | Execution boundaries from agent lifecycle and local intent return. |
| Offline reporter | `scripts/report_voice_task_metrics.py` | Joins saved journal/AA events and reports KPI-3 without network access. |
| HAL pipe | `hal/telemetry/client.py` | Generic: local log line, bounded queue, background POST. Reusable by future trackers. |
| OS ingestion | `system/server/telemetry/delivery/http/handler.go` | `POST /api/telemetry/event` (loopback/LAN, same gate as `/api/sensing/event`). |
| OS pipe | `system/telemetry/telemetry.go` | Common fields, de-duplication, bounded queue, one sender, local log line. |
| Transport | `system/lib/analytics` | Autonomous Analytics `event_tracking` client (shared with web/mobile). |

Voice code is touched only at the boundaries it already owns: speech endpoint,
turn dispatch, TTS playback start/end, and the cancel gesture. The rules,
thresholds and event shapes all live in the two tracking packages.

## Interaction id

An interaction is **one user utterance**. `voice_metrics.speech_end()` creates
`interaction_id` (`vi-<16 hex>`) at the moment HAL decides the user stopped
speaking, and it is carried through realtime, the sensing POST (bound to the
os-server `runId` returned by `/api/sensing/event`), the main agent's reply
(via the TTS queue `turn_id`), fillers and playback.

`voice_agent_handled` is a **backend notification about an interaction that
was already answered**, not a new one: it binds to the same `interaction_id`,
so a realtime-handled turn produces exactly one KPI-1 sample.

Each **queued segment** of a streamed reply is its own measured playback: the
drain re-arms the hook under that segment's own owner, because it plays on the
audio stream whichever turn spoke first had opened.

**Ownership is explicit, never guessed.** Every playback carries the owner
that claimed the speaker: `run:<turn_id>` for an agent reply or a filler armed
for that turn (os-server passes the run id down with the filler, and back
through `/api/sensing/filler`'s `owner` field for the realtime wait filler),
and `interaction:<id>` for realtime native voice. The realtime branch that
answers through TTS instead of native audio tags its speech the same way —
before os-server has issued any run id, the interaction id itself is the tag. Audio that nobody claimed is
recorded as `unknown` and **never counts as an acknowledgement** — guessing
"the newest open interaction" is how an old filler gets credited to a new
command. The count rides on every interaction row as
`unknown_owner_playbacks`.

A muted playback also needs a resolvable owner to exclude an interaction as
`speaker_muted`. An unrelated or unowned muted notice cannot exclude the
newest voice command. Queued segments carry their own classification metadata
(answer/filler/system), so they do not inherit the kind of the speech that
opened their shared stream. These snapshots do not change feedback or
interruption behavior.

## What counts as an acknowledgement

The first frame **actually written to the audio stream**
(`TTSService._note_audio_written`, called from every real write path: streamed
synth, the queued-sentence drain, cached WAV playback, and realtime native
frames).

`on_speak_start` is **not** that signal and is not used for the KPI: the
cached path fires it before taking the stream lock and before writing
anything, so speech that is stopped or fails in between would look played.
Neither does HTTP acceptance — `speak_queue()` deliberately returns `True` for
requests it drops (superseded turn), and os-server's `deliverTTS` can be muted
after accepting text.

Even the first write is **not the acoustic onset**: ALSA (and more so
Bluetooth) buffering sits between the write and the speaker. The measurement
is "audio was handed to the device", not "sound left the driver".

| Playback kind | Modality recorded | How it is classified |
|---------------|-------------------|----------------------|
| `native_realtime` | `spoken_answer_realtime` | written by the native-frame path — realtime model's own voice |
| `realtime_tts` | `spoken_answer_realtime` | realtime text answer synthesized through TTS, identified by playback metadata independently of `realtime_feedback` |
| `agent_reply` | `spoken_answer` | `realtime_feedback` (only the agent's reply sets it) |
| `waiting_audio` | `waiting_audio` | interruptible cached speech, i.e. a dead-air filler |
| `system_audio` | `acknowledgement_audio` | any other cached/system phrase |
| `unknown` | — (never an ack) | nobody claimed the speaker for this playback |

The physical-gesture acknowledgement chime is not a voice-command receipt.
Its writes bypass the speech measurement hook: they neither inherit a pending
reply's owner nor consume its first-frame hook.

**Waiting audio counts as an acknowledgement — decided, not accidental.**
The question this metric answers is *"did the device let the user know it
heard them?"*, not *"did it answer?"*. A filler ("one moment") is a real
receipt: the user stops wondering whether they were heard. So a turn whose
first sound is a filler is acknowledged, even if the answer itself arrives
much later.

The consequence has to be read with it: on lamp today **every** measured
acknowledgement is `waiting_audio`, so this metric currently reports how fast
the device says "I heard you", NOT how fast it answers. `ack_modality` is
stored on every row precisely so the two can be told apart — split by it
before quoting a number, and treat a 100 % `waiting_audio` split as a finding
about the product, not as a good score.

**Visual feedback is not counted.** The listening LED is a real receipt
signal, but its actual activation is not instrumented here; counting it
without measuring it would overstate KPI-1. Adding it means instrumenting
`_set_emotion_local(EMO_LISTENING)` and reporting `ack_modality =
"visual_cue"` explicitly.

## Clocks

Every duration is a difference of two `time.monotonic()` reads **inside the
HAL process**. HAL and os-server monotonic clocks are never mixed. Wall-clock
`event_timestamp` (seconds) from the analytics client is not used for latency
— all durations are explicit millisecond fields.

`speech_end_method` records how the endpoint was decided, because it is a
**detection**, not the acoustic truth: `silence_clock`, `tts_started`,
`music_started`, `max_duration`, `stt_error`. The clock starts at that
detection (the moment the capture loop broke), **not** after
`finalize_session` — transcript assembly, trailing-silence trim and speaker-ID
run in between and would otherwise be charged to the device's response time.

## Events

### `voice_metrics_interaction` — utterance snapshots (KPI-1 and KPI-3)

| Field | Meaning |
|-------|---------|
| `interaction_id`, `run_id`, `event_type`, `route` | Identity + how the turn was routed (`handled` / `delegated` / fallback / noise-dropped) |
| `speech_end_method` | How the endpoint was detected |
| `eligible` | `false` when `exclusion_reason` is set |
| `outcome` | `acknowledged` \| `no_ack` \| `excluded` |
| `exclusion_reason` | `rejected_noise`, `rejected_non_user`, `no_transcript`, `not_addressed`, `speaker_muted`, `interrupted_by_user`. `speaker_muted` is recorded when the speaker actually **refuses** the speech, not by sampling the mute flag later — otherwise a device unmuted before scoring looks like one that simply never answered |
| `failure_reason` | `dispatch_failed` — the command was valid and went **unserved** (the POST never landed). This is *not* an exclusion: the row stays eligible and counts against the KPI. A command os-server answered itself (local intent: volume, LED, time) is **not** a failure — its reply carries the interaction id as owner and counts as answered. |
| `ack_latency_ms` | Raw observation, kept whatever the verdict (`null` when nothing played) |
| `ack_modality`, `ack_kind` | What the user actually heard |
| `answer_latency_ms`, `answer_kind` | When the **answer** was heard (`agent_reply` / `native_realtime` / `realtime_tts`), as opposed to the receipt. `null` = acknowledged but never answered inside the window — a finding, not missing data |
| `ack_deadline_ms`, `observe_window_ms` | 3000 / 10000 — the (provisional) thresholds in force when the row was written |
| `unknown_owner_playbacks` | Playbacks nobody claimed so far — audio excluded from ack decisions |
| `amends_event_id`, `amendment_reason` | Set on a **correction** row: routing or an exclusion arrived after the verdict was sent |

The observation window is **10 s**, wider than the 3 s target on purpose: a
late answer is reported with its real latency instead of collapsing into "no
answer at all", so the threshold can be re-decided from stored data.

**Reporting the verdict does not end the turn.** The KPI-1 row is written at
10 s; the main agent may still be working, and a stop pressed at second 12
must still find that turn to suppress. A turn stays *active* until it has been
silent for `TURN_ACTIVE_TTL_MS` (45 s) — a clock any playback of that turn
restarts, and which never expires while that turn's audio is still playing —
or until it is excluded or fails. Only then does it leave the
KPI-2 denominator. A verdict that turns out wrong afterwards is corrected with
an amendment row.

### `voice_metrics_suppression` — one per boundary (KPI-2)

| Field | Meaning |
|-------|---------|
| `suppression_reason` | `explicit_stop` (user clicked) or `auto_supersede` (realtime answered a newer utterance) |
| `interaction_id` | The utterance that triggered the boundary |
| `applicable_interactions` | How many older in-flight interactions the boundary covers |
| `old_audio_playing_at_boundary` | Whether old audio was already playing when the boundary was stamped |
| `stale_observed` | **KPI-2 numerator** |
| `stale_kind`, `stale_started_after_ms` | Which audio, and how long after the boundary it started (negative-free: audio already playing counts too) |
| `stale_audible_past_grace_ms` | How far past the grace old audio was still audible |
| `stop_to_silence_ms` | How long old audio kept playing after the boundary — recorded raw, even inside the grace |
| `grace_ms`, `observe_window_ms` | 2000 / 60000 |
| `observation_complete` | `false` when the window closed while suppressed turns could still speak — such a row is **not** evidence of a clean suppression |
| `unobserved_interactions` | How many suppressed turns were still alive at that point |

**Stale means audible past the grace, whether it started then or merely kept
going.** Each playback interval is scored against every open boundary, so
audio that began before the stop and ran past the grace counts — an earlier
version only looked at playbacks that *started* after the boundary and passed
a reply that was still talking three seconds after a stop.

**The window is long, and an unfinished observation says so.** A main-agent
reply can arrive tens of seconds after the boundary, so a suppression is
watched for 60 s. If that expires while a suppressed turn can still speak, the
row reports `observation_complete = false` instead of "no stale reply".
If stale audio was already observed, it remains a confirmed failure even when
the observation is incomplete; more observation cannot undo that playback.

**The grace period is documented, not invented to pass.** `TTSService.stop()`
sets a stop event and wakes the drain queues; the worker still has to release
the lock. The code comment in `hal/drivers/voice/tts/service.py` records a
device-observed pathological case of ~4 s on lamp-0c89 (sub-second normally),
which is why the wake-the-queues fix exists. `STALE_GRACE_MS = 2000` is the
reporting choice; `stop_to_silence_ms` is stored raw on every boundary so the
choice stays auditable and can be changed without re-instrumenting devices.

Both boundaries are stamped **in HAL**, where both originate: the cancel
gesture is a HAL button action, and `voice_agent_handled` is emitted by HAL's
turn dispatcher.

**A boundary is recorded only when os-server actually applied it.** The
`/api/sensing/event` response to a `voice_agent_handled` post carries
`speechSuppressed` — os-server's own answer to "did I take the speaker away
from the older turn". With `OS_REALTIME_SUPERSEDES_MAIN_REPLY` off (the
default) nothing is suppressed, and a failed POST suppresses nothing either;
neither case is recorded, because a denominator full of situations where
nothing was ever suppressed makes KPI-2 look good for free.

**The denominator counts active turns only.** An interaction that was already
excluded or closed cannot produce a stale reply, so it is not counted as
something the boundary had to suppress.

## KPI definitions

**KPI-1 — acknowledged within 3 s**

- Denominator: `voice_metrics_interaction` rows with `eligible = true`, after
  applying amendments (a row whose `interaction_id` also has a row with
  `amends_event_id` set is superseded by that correction).
- Numerator: those with `outcome = 'acknowledged'` and `ack_latency_ms <= 3000`.
- Excluded (reported, never dropped): noise-rejected turns, turns the realtime
  model explicitly rejected as non-user, empty transcripts, utterances not
  addressed to the device (no wake word / outside the follow-up window), and a
  muted speaker.
- **Failures stay in.** A valid command the device never served (the POST to
  os-server did not land — `failure_reason = 'dispatch_failed'`) is *eligible*
  and counts as `no_ack`. Excluding it would inflate the success rate with
  exactly the cases the user feels most. Slow interactions stay in too.

**KPI-2 — outdated reply actually played**

- Denominator: `voice_metrics_suppression` rows with `applicable_interactions > 0`
  **and** (`observation_complete = true` **or** `stale_observed = true`). A
  confirmed stale playback stays in both numerator and denominator even if
  suppressed turns are still active when the observation window closes.
  Only incomplete observations with `stale_observed = false` are reported
  separately as coverage loss — never folded in as passes.
- Numerator: those with `stale_observed = true`.
- Report `explicit_stop` and `auto_supersede` separately: they are different
  policies. Automatic supersession only happens when os-server has
  `OS_REALTIME_SUPERSEDES_MAIN_REPLY=1`. **The code default is OFF and this
  change does not alter it** — but read the body's own `.env` before drawing
  conclusions: lamp ships with it **enabled**
  (`robots/lamp/rootfs/opt/hal/.env`), so lamp does produce `auto_supersede`
  samples. On a body where it is off the denominator is empty and that slice
  is **N/A**, not 100 %.
- Correct suppression (`observation_complete = true`, `stale_observed = false`) is a **pass**, not a missing
  response. Hardware actions of an auto-superseded turn remain valid by
  design; only speech and fillers are dropped.
- No eligible samples ⇒ **N/A**. Never report 0 % or 100 % from an empty set.

## KPI-3: execution completion, not correctness

“Successfully complete” means **the task finished executing without observed terminal execution errors**. An agent may
finish with an incorrect answer or an ineffective action and still meet this
execution metric. It does not assess user satisfaction, tool-result correctness,
physical effect, or whether the entire spoken answer played.

The HAL utterance is the cohort; backend runs alone are never voice samples.
`voice_metrics_interaction` adds these independent task fields:

| Field | Meaning |
|-------|---------|
| `task_schema_version` | `1`; older rows lack task instrumentation and are coverage loss, not scored failures |
| `task_started_at_ms` | Unix milliseconds at detected speech end; selects the report cohort, not a cross-process latency |
| `task_revision` | Increasing snapshot revision; keep the greatest revision per device + interaction, including late run-id bindings |
| `task_eligible` | Independent of acknowledgement eligibility: mute and speech interruption do not exclude execution |
| `task_eligibility_known` | Whether routing/exclusion has been observed; report unknown coverage explicitly |
| `task_exclusion_reason` | Noise, non-user, empty transcript, or not-addressed rejection; dispatch failure stays eligible |

`voice_metrics_task_execution` carries `schema_version=1`, `run_id`,
`interaction_id`, `outcome` (`completed`, `failed`, `unknown`), `evidence`,
`execution_at_ms` (Unix milliseconds), and a boolean `error`. It carries no
error text, transcript or tool output. Evidence is:

| Evidence | Outcome and interpretation |
|----------|----------------------------|
| `lifecycle_end` | `completed`: the runtime explicitly ended execution without `data.aborted=true` or a nonempty `data.error` |
| `lifecycle_end_error` | `failed`: even a lifecycle end is a failure when aborted or carrying an error; `stopReason` alone is not interpreted |
| `lifecycle_error`, `lifecycle_end_error`, `chat_error`, `local_intent_error` | `failed`: execution failed; local intent observed a HAL action API error |
| `lifecycle_error_recovered` | `unknown`: recovery alone is not completion; wait for explicit end |
| `local_intent_returned` | `completed`: the local intent handler finished without an observed HAL action error; this does not prove semantic correctness or the physical effect |
| `realtime_turn_done` | `completed`: the provider emitted TurnDoneEvent and HAL returned a handled turn |

Join execution evidence to the cohort by **device + run_id** or **device +
interaction_id**. OS observations also include non-voice runs: never put those
in the denominator by themselves. For `route=realtime_handled`, accept only
`realtime_turn_done`; ignore any backend memory-sync run. Any joined `failed` terminal evidence through as-of is sticky: a later
`lifecycle_end` cannot erase it, because that end may only reflect cleanup.
Otherwise use the latest execution timestamp through as-of; equal timestamps
prefer completed over unknown. A recovered error is `unknown`, so a later
genuine end can establish completion. This is about terminal errors, not every
tool attempt: a handled tool error may recover without terminal failure.
Dispatch failure without execution evidence counts as failed. `Result.ExecutionFailed` records
local-intent HAL API errors that were previously only logged, so returning from
the handler after such an error emits `local_intent_error`, not a completion.

The provisional, configurable default reporting horizon is **1800 seconds**: the denominator includes
eligible turns with `task_started_at_ms <= as_of_ms - 1800000`. Fresher turns
are reported separately as `fresh_pending_turns`, even if already completed.
Mature failed, unknown, and incomplete turns **stay in the denominator**;
missing eligibility knowledge is flagged rather than silently removed.
The numerator is mature turns whose selected evidence says `completed`.
Compare the unrounded fraction to **85%**; an empty denominator is **N/A**
(`kpi3_pct` and `meets_target` are JSON `null`).

This horizon is a reporting choice, **not an execution timeout**. Neither the
10-second ack observation nor the 45-second speech-active TTL ends a task.
A late completion is counted when the report is rerun with later evidence.
Always collect execution results through the stated as-of time; do not cut
results off at the cohort's last speech timestamp.

## Agent runbook: query and report KPI-3

On an authorized device session, export both services with hostname metadata:

```bash
journalctl -u hal -u os-server --since '7 days ago' -o json --no-pager > /tmp/voice-task-journal.jsonl
```

Copy that file to the workstation using the already authorized device access,
then run from the repo root (or pipe the export to stdin):

```bash
python3 scripts/report_voice_task_metrics.py voice-task-journal.jsonl > voice-task-report.json
python3 scripts/report_voice_task_metrics.py aa-export.jsonl --now-ms 1789088400000 --settle-seconds 1800 > voice-task-report.json
```

`--now-ms` fixes an as-of Unix millisecond timestamp for reproducibility; use
the actual export cutoff. The reporter filters observations before selecting
revisions or loss counters, using journal `__REALTIME_TIMESTAMP` (microseconds),
AA `event_timestamp` (seconds), or normalized `observed_at_ms` (milliseconds).
Rows after the cutoff are counted in `future_events_excluded`. Untimestamped
exports remain usable but increment `missing_observation_timestamp_events`;
they cannot establish a historically accurate snapshot, so use an export
already cut at the requested time. The CLI reads JSONL incrementally and retains
metric state rather than loading the entire journal into memory.
`--device <hostname>` supplies a fallback for exports
without device identity; it is **not a device filter**. Journal JSONL (including ANSI-colored messages and journald `MESSAGE` byte
arrays) and AA JSONL with `event_name`, `data.user_pseudo_id`, and `data.event_params` key/value
pairs are accepted. Preserve amendments and execution events in the export.
The reporter groups by device and deduplicates HAL/OS copies by task revision.
Smoke interactions beginning `vi-smoke-` are excluded by default;
`--include-synthetic --settle-seconds 0` is only for instrumentation checks,
never a production KPI claim.

Report the interval, as-of time, settling horizon, source (local logs or AA),
per-device and aggregate `completed_turns / eligible_mature_turns`, percent,
and `meets_target`. Include `failed_turns`, `unknown_turns`, `incomplete_turns`,
`fresh_pending_turns`, `ineligible_turns`, legacy/missing-start exclusions and
unknown-eligibility coverage. State **execution finished; correctness was not
assessed**. Report malformed/missing-identity coverage and telemetry loss
counters too; local logs can contain events absent from AA. The reporter uses
per-device maxima for `hal_dropped_total`, `hal_failed_total`,
`telemetry_dropped_total`, `telemetry_failed_total`, and
`unknown_owner_playbacks`, then sums device maxima for the aggregate. These
are coverage indicators, not an exact loss count across restarts/interactions;
inspect reset boundaries before interpreting totals.

An INFO `[telemetry] delivered` line for `voice_metrics_*` proves that the AA
HTTP send succeeded. A local `[telemetry] event` line proves only local
recording. Neither proves warehouse query visibility. Warehouse verification
requires read access and a query/export showing the matching `event_id` and
interaction/run identity; the AA ingestion credential alone is not a read API.

## Warehouse queries

Schema assumption (labelled, because the AA warehouse schema is not in this
repo): events land in a table `event_tracking` with `event_name`,
`event_timestamp`, and `event_params` as an array of `{key, value}` pairs with
`value` stored as a string — the shape this device posts and the shape web and
mobile already post. Adjust the extraction helper if your warehouse flattens
params into columns.

```sql
-- Helper: pull one param out of the key/value array.
-- BigQuery-style; replace with your warehouse's array accessor.
CREATE TEMP FUNCTION param(params ANY TYPE, k STRING) AS (
  (SELECT p.value FROM UNNEST(params) p WHERE p.key = k LIMIT 1)
);
```

```sql
-- KPI-1: acknowledged within 3s, with the eligible sample count.
WITH rows AS (
  SELECT
    param(data.event_params, 'interaction_id')     AS interaction_id,
    param(data.event_params, 'eligible')           AS eligible,
    param(data.event_params, 'outcome')            AS outcome,
    param(data.event_params, 'amends_event_id')    AS amends,
    SAFE_CAST(param(data.event_params, 'ack_latency_ms') AS INT64) AS ack_ms,
    event_timestamp
  FROM event_tracking
  WHERE event_name = 'voice_metrics_interaction'
    AND event_timestamp >= UNIX_SECONDS(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY))
),
-- One row per interaction: the latest verdict wins, so a correction
-- (amends_event_id set) supersedes the row it amends.
i AS (
  SELECT * EXCEPT(rn) FROM (
    SELECT *, ROW_NUMBER() OVER (
      PARTITION BY interaction_id
      ORDER BY IF(amends != '', 1, 0) DESC, event_timestamp DESC
    ) AS rn
    FROM rows
  ) WHERE rn = 1
),
eligible AS (SELECT * FROM i WHERE eligible = 'true')
SELECT
  COUNT(*) AS eligible_samples,
  COUNTIF(outcome = 'acknowledged' AND ack_ms <= 3000) AS acknowledged_within_3s,
  CASE WHEN COUNT(*) = 0 THEN NULL      -- no eligible samples => N/A
       ELSE ROUND(100 * COUNTIF(outcome = 'acknowledged' AND ack_ms <= 3000) / COUNT(*), 2)
  END AS kpi1_pct
FROM eligible;
```

```sql
-- How long users actually wait for the ANSWER, not for the "one moment".
-- Read next to KPI-1: a fast ack with a null answer means the device was
-- polite, not useful.
SELECT
  COUNT(*)                                                   AS eligible,
  COUNTIF(param(data.event_params, 'answer_latency_ms') IS NULL) AS acked_but_never_answered,
  APPROX_QUANTILES(SAFE_CAST(param(data.event_params, 'answer_latency_ms') AS INT64), 100)[OFFSET(50)] AS median_answer_ms,
  APPROX_QUANTILES(SAFE_CAST(param(data.event_params, 'answer_latency_ms') AS INT64), 100)[OFFSET(90)] AS p90_answer_ms
FROM event_tracking
WHERE event_name = 'voice_metrics_interaction'
  AND param(data.event_params, 'eligible') = 'true';
```

```sql
-- KPI-2: stale playback per suppression policy.
WITH s AS (
  SELECT
    param(data.event_params, 'suppression_reason')  AS reason,
    param(data.event_params, 'stale_observed')      AS stale,
    param(data.event_params, 'observation_complete') AS complete,
    SAFE_CAST(param(data.event_params, 'applicable_interactions') AS INT64) AS applicable
  FROM event_tracking
  WHERE event_name = 'voice_metrics_suppression'
    AND event_timestamp >= UNIX_SECONDS(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY))
)
SELECT
  reason,
  -- Confirmed stale playback counts even before observation is complete.
  -- Only incomplete observations without stale playback are coverage loss.
  COUNTIF(applicable > 0 AND (complete = 'true' OR stale = 'true')) AS eligible_samples,
  COUNTIF(applicable > 0 AND stale = 'true') AS stale_played,
  COUNTIF(applicable > 0 AND complete = 'false' AND stale = 'false') AS incomplete_observations,
  CASE WHEN COUNTIF(applicable > 0 AND (complete = 'true' OR stale = 'true')) = 0 THEN NULL
       ELSE ROUND(100 * COUNTIF(applicable > 0 AND stale = 'true')
                      / COUNTIF(applicable > 0 AND (complete = 'true' OR stale = 'true')), 2)
  END AS kpi2_pct
FROM s
GROUP BY reason;
```

```sql
-- Telemetry coverage: a climbing loss counter means the rates above are
-- computed on partial data.
SELECT
  MAX(SAFE_CAST(param(data.event_params, 'telemetry_dropped_total') AS INT64)) AS os_dropped,
  MAX(SAFE_CAST(param(data.event_params, 'telemetry_failed_total')  AS INT64)) AS os_failed,
  MAX(SAFE_CAST(param(data.event_params, 'hal_dropped_total')      AS INT64)) AS hal_dropped,
  MAX(SAFE_CAST(param(data.event_params, 'hal_failed_total')       AS INT64)) AS hal_failed,
  MAX(SAFE_CAST(param(data.event_params, 'unknown_owner_playbacks') AS INT64)) AS unowned_playbacks
FROM event_tracking
WHERE event_name LIKE 'voice_metrics_%';
```

### KPI-3 warehouse extraction and scoring

Use the same schema assumption and `param` helper above. This BigQuery-style
example selects a seven-day speech cohort and reads evidence through as-of.
Set the actual export/query cutoff and device filter in `raw` when needed.
Do not filter execution rows to mature speech timestamps. Partial realtime
output without provider `TurnDoneEvent` remains incomplete.

```sql
WITH config AS (
  SELECT UNIX_MILLIS(CURRENT_TIMESTAMP()) AS as_of_ms, 1800000 AS settle_ms
), raw AS (
  SELECT event_name, event_timestamp, data.user_pseudo_id AS device,
    data.event_params AS p
  FROM event_tracking, config
  WHERE event_name IN ('voice_metrics_interaction', 'voice_metrics_task_execution')
    AND event_timestamp <= DIV(as_of_ms, 1000)
    AND NOT STARTS_WITH(COALESCE(param(data.event_params, 'interaction_id'), ''), 'vi-smoke-')
), snapshots AS (
  SELECT device, param(p, 'interaction_id') AS interaction_id,
    param(p, 'run_id') AS run_id, param(p, 'route') AS route,
    param(p, 'task_schema_version') AS version,
    param(p, 'task_eligible') AS eligible,
    param(p, 'task_eligibility_known') AS eligibility_known,
    param(p, 'failure_reason') AS failure_reason,
    SAFE_CAST(param(p, 'task_started_at_ms') AS INT64) AS started_ms,
    ROW_NUMBER() OVER (
      PARTITION BY device, param(p, 'interaction_id')
      ORDER BY COALESCE(SAFE_CAST(param(p, 'task_revision') AS INT64), 0) DESC,
               event_timestamp DESC
    ) AS revision_rank
  FROM raw WHERE event_name = 'voice_metrics_interaction'
), cohort AS (
  SELECT s.* FROM snapshots s, config
  WHERE revision_rank = 1
    AND COALESCE(device, '') != '' AND COALESCE(interaction_id, '') != ''
    -- Legacy/missing-start rows retained for coverage, not scoring.
    AND (started_ms BETWEEN as_of_ms - 7*86400000 AND as_of_ms OR started_ms IS NULL)
), executions AS (
  SELECT device, param(p, 'interaction_id') AS interaction_id,
    param(p, 'run_id') AS run_id, param(p, 'outcome') AS outcome,
    param(p, 'evidence') AS evidence,
    SAFE_CAST(param(p, 'execution_at_ms') AS INT64) AS execution_ms
  FROM raw, config
  WHERE event_name = 'voice_metrics_task_execution'
    AND param(p, 'schema_version') = '1'
    AND SAFE_CAST(param(p, 'execution_at_ms') AS INT64) <= as_of_ms
), joined AS (
  SELECT c.*, e.outcome AS execution_outcome,
    COUNTIF(e.outcome = 'failed') OVER (
      PARTITION BY c.device, c.interaction_id
    ) AS terminal_errors,
    ROW_NUMBER() OVER (
      PARTITION BY c.device, c.interaction_id
      ORDER BY e.execution_ms DESC,
        CASE e.outcome WHEN 'failed' THEN 2 WHEN 'completed' THEN 1 ELSE 0 END DESC
    ) AS evidence_rank
  FROM cohort c LEFT JOIN executions e
    ON c.device = e.device
    AND (c.interaction_id = e.interaction_id
         OR (COALESCE(c.run_id, '') != '' AND c.run_id = e.run_id))
    AND (COALESCE(c.route, '') != 'realtime_handled' OR e.evidence = 'realtime_turn_done')
), scored AS (
  SELECT *, version = '1' AND COALESCE(eligible, '') != 'false'
      AND started_ms <= as_of_ms - settle_ms AS mature,
    IF(terminal_errors > 0, 'failed', COALESCE(execution_outcome,
      IF(COALESCE(failure_reason, '') != '', 'failed', 'incomplete'))) AS task_outcome
  FROM joined, config WHERE evidence_rank = 1
)
SELECT device,
  COUNTIF(mature) AS eligible_mature_turns,
  COUNTIF(mature AND task_outcome = 'completed') AS completed_turns,
  COUNTIF(mature AND task_outcome = 'failed') AS failed_turns,
  COUNTIF(mature AND task_outcome = 'unknown') AS unknown_turns,
  COUNTIF(mature AND task_outcome = 'incomplete') AS incomplete_turns,
  COUNTIF(version = '1' AND COALESCE(eligible, '') != 'false'
    AND started_ms > as_of_ms - settle_ms) AS fresh_pending_turns,
  COUNTIF(version = '1' AND eligible = 'false') AS ineligible_turns,
  COUNTIF(COALESCE(version, '') != '1') AS legacy_turns_excluded,
  COUNTIF(version = '1' AND started_ms IS NULL) AS missing_start_time_turns_excluded,
  COUNTIF(version = '1' AND COALESCE(eligible, '') != 'false'
    AND COALESCE(eligibility_known, '') != 'true') AS eligibility_unknown_turns,
  100 * SAFE_DIVIDE(COUNTIF(mature AND task_outcome = 'completed'), COUNTIF(mature)) AS kpi3_pct,
  IF(COUNTIF(mature) = 0, NULL,
    100 * COUNTIF(mature AND task_outcome = 'completed') >= 85 * COUNTIF(mature)) AS meets_target
FROM scored GROUP BY device;
```

Count empty device/interaction identities separately before scoring and repair
the export; do not merge them into a fabricated device. For aggregate KPI,
sum completed and eligible counts across devices and divide those sums; do not
average percentages. For local reports the input files define the interval
(no start-window CLI); keep all relevant amendments and the execution tail.
Legacy rows without start timestamps need an export-time interval for meaningful
coverage counts. Loss counters are cumulative per process, may reset on restart,
and must not be summed per event. Inspect their maxima and reset boundaries:

```sql
SELECT data.user_pseudo_id AS device,
  MAX(SAFE_CAST(param(data.event_params, 'telemetry_dropped_total') AS INT64)) AS os_dropped_max,
  MAX(SAFE_CAST(param(data.event_params, 'telemetry_failed_total') AS INT64)) AS os_failed_max,
  MAX(SAFE_CAST(param(data.event_params, 'hal_dropped_total') AS INT64)) AS hal_dropped_max,
  MAX(SAFE_CAST(param(data.event_params, 'hal_failed_total') AS INT64)) AS hal_failed_max
FROM event_tracking
WHERE event_name IN ('voice_metrics_interaction', 'voice_metrics_task_execution')
  AND event_timestamp >= UNIX_SECONDS(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY))
GROUP BY device;
```

## Local logs on the device

Every event is logged **before** it is sent, and delivery failures are logged
too — the warehouse copy is the one that can be missing.

```bash
journalctl -u hal -f | grep '\[telemetry\]'        # HAL: every event + POST failures
journalctl -u hal -f | grep '\[voice-metrics\]'       # ack / boundary / stale decisions
journalctl -u os-server -f | grep '\[telemetry\]'  # os-server: forwarded, dropped, failed
```

Match rows by `interaction_id`, never by adjacency. `Session END` and the
`[turn] route=` line include that id; the interaction verdict appears after
the 10-second observation timer and may sit between logs of a later session.
HAL's telemetry JSON also includes `event_id` locally, so amendments can be
matched to their original event even when analytics sending is disabled.

```bash
journalctl -u hal -o cat --no-pager | grep -F 'vi-<interaction-id>'
```

`eligible=false` with `ack_latency_ms=null` is an excluded sample; check
`exclusion_reason`. `eligible=true` with a null ack is a missed acknowledgement
inside the observation window. A nonempty transcript alone does not establish
eligibility: the wake-word gate or realtime non-user rejection can still
exclude it. Transcript text stays in the existing local voice logs and is
never added to telemetry payloads.

## Configuration

Both values live in the body's `/opt/hal/.env`, which os-server loads at
startup — a device can be pointed at a different warehouse without a rebuild:

| Key | Meaning |
|-----|---------|
| `AUTONOMOUS_ANALYTICS_URL` | Where events are posted — **and the on/off switch**. Empty (the default) = nothing leaves the device. There is no built-in fallback endpoint. |
| `AUTONOMOUS_ANALYTICS_ID` | AA authorization key. Missing ⇒ delivery fails loudly (`[telemetry] delivery failed`). |

Both processes (HAL and os-server) read the same key, per call — filling it
in plus a service restart is enough, no rebuild. Events are written to the
local log either way; an empty URL only skips the network hop.

Resolution order: process env → `/opt/hal/.env`. The HAL→os-server hop
(`http://127.0.0.1:5000/api/telemetry/event`) stays a loopback constant like
every other HAL→OS call.

## Privacy

Transcripts, reply text, raw audio and credentials are never sent. Only ids,
enumerated outcomes, and durations. `user_pseudo_id` is the device hostname
and `platform` is `device` (see `system/lib/analytics`).

## Limitations

- **Detected endpoint, not acoustic end.** Latency is measured from the
  silence clock / STT final, not from the physical end of speech.
- **Visual acknowledgement is not measured** (see above).
- **`auto_supersede` depends on the body.** The code default for
  `OS_REALTIME_SUPERSEDES_MAIN_REPLY` is OFF (unchanged here), but lamp enables
  it in its own `.env`; a body with it off produces no samples for that slice.
- **Stale detection is playback-level, not sample-level.** It observes audio
  intervals crossing the grace boundary; it cannot say how many milliseconds
  of PCM were still in the hardware buffer.
- **Audio handed to the driver, not sound in the room.** The ack point is the
  first stream write; ALSA/Bluetooth buffering after it is not measured.
- **Unclaimed playback is unattributed by design.** It is counted
  (`unknown_owner_playbacks`) and excluded from ack decisions rather than
  guessed at.
- **A 60 s suppression window can still expire early.** Those rows carry
  `observation_complete = false`. Confirmed stale playback still counts as a
  failure; only rows without observed stale playback are coverage loss.
- **In-memory state only.** A HAL restart loses interactions still in their
  observation window; those samples are missing rather than wrong.
- **Tracker capacity is 32 interactions.** An older one evicted before its
  verdict was due is reported early, flagged `eviction = 'tracker_capacity'`,
  rather than dropped — its observation window was cut short.
- **Delivery loss is reported, not hidden**: `telemetry_dropped_total`,
  `telemetry_failed_total`, `hal_dropped_total`, `hal_failed_total` ride on
  every event.

## Validation

```bash
go build ./...                                   # os-server + tracking package
go test ./system/telemetry/ ./system/server/telemetry/...
make hal-lint
cd hal && .venv/bin/python -m pytest test/test_voice_metrics.py -q
```

## Device validation — 2026-09-11

Validated on `lamp-0c89` (`172.168.20.169`), version
`0.1.88-voice-task`, using authorized deployment and curl smoke requests.
No transcript or full device journal is stored in this repo.

| Observation | Correlation and result |
|-------------|------------------------|
| Synthetic local time intent | `vi-smoke-c5e84236c9` → `local_intent_returned`, event `vte-RAUD26IPOSIDNMCK3LF4F3PRVJ` |
| Synthetic delegated arithmetic | `vi-smoke-09bf638baa` → run `device-chat-4-1789090404539` → `lifecycle_end` completed, event `vte-CQ54WS3EVMVLQRQPSP6FPYCITP`; AA delivered log at 08:33:29 +07 |
| Natural realtime turn | `vi-85eff37e57264e94` → `realtime_turn_done`, event `task-rt-vi-85eff37e57264e94`; AA delivered log at 08:34:11 +07 |

The synthetic-only offline report was **2/2** with
`--include-synthetic --settle-seconds 0`: an instrumentation check, not evidence
that production meets 85%. At the 08:35 +07 snapshot, the normal 1800-second
report was **N/A**, with two fresh pending turns. AA HTTP send success was
verified; no warehouse read query was performed.

Device rollback copies: `/tmp/os-server-before-voice-task` and
`/tmp/voice-task-hal-before.tgz`. These paths are on the device, not repo files.
Local validation covered focused Go domain, intent, telemetry and handler tests,
75 focused HAL tests, and 20 reporter unittest cases. HAL lint used a temporary
test venv under `/tmp` with pyflakes. The full HAL suite is not claimed green:
`test_gemini_generation_complete.py:52` has a baseline `InterruptedOutput`
versus expected `TextOutput` failure, also reproduced against HEAD.

Commands used for this validation (the temporary venv included pytest and
pyflakes; the final ARM64 binary was installed and os-server restarted active):

```bash
go test ./system/domain ./system/intent ./system/telemetry ./system/server/telemetry/... ./system/server/agent/delivery/http ./system/server/sensing/delivery/http
/tmp/voice-task-test-venv/bin/python -m pytest hal/test/test_realtime_execution_completed.py hal/test/test_voice_task_metrics.py hal/test/test_voice_metrics.py hal/test/test_voice_metrics_queries.py hal/test/test_voice_metrics_ownership.py -q
python3 -m unittest discover -s scripts/tests -p 'test_report_voice_task_metrics.py'
/tmp/voice-task-test-venv/bin/python hal/scripts/lint.py
GOOS=linux GOARCH=arm64 go build -ldflags '-s -w -X go.autonomous.ai/os/system/server/config.OSVersion=0.1.88-voice-task' -o /tmp/os-server-voice-task ./system/cmd/os-server
```
