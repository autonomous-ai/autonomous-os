# Voice KPI tracking

Measurement only. Nothing in this document changes what the device says, when
it speaks, or when it stays quiet — it records what already happened so two
numbers can be computed from real devices:

| KPI | Question | Target |
|-----|----------|--------|
| **KPI-1** | Share of eligible voice interactions acknowledged within **3 s** of the detected end of the user's speech | ≥ 95 % |
| **KPI-2** | Share of eligible suppression boundaries where an **outdated** reply was actually played | < 1 % |

Both targets are **provisional**. Every raw duration is stored, so a threshold
can be re-decided from the data instead of by re-instrumenting devices.

## Where the code lives

| Layer | Path | Role |
|-------|------|------|
| HAL tracker | `hal/telemetry/voice_metrics.py` | All measurement. Owns interaction ids, ack detection, stale-playback detection. |
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
| `agent_reply` | `spoken_answer` | `realtime_feedback` (only the agent's reply sets it) |
| `waiting_audio` | `waiting_audio` | interruptible cached speech, i.e. a dead-air filler |
| `system_audio` | `acknowledgement_audio` | any other cached/system phrase |
| `unknown` | — (never an ack) | nobody claimed the speaker for this playback |

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

### `voice_metrics_interaction` — one per utterance (KPI-1)

| Field | Meaning |
|-------|---------|
| `interaction_id`, `run_id`, `event_type`, `route` | Identity + how the turn was routed (`handled` / `delegated` / fallback / noise-dropped) |
| `speech_end_method` | How the endpoint was detected |
| `eligible` | `false` when `exclusion_reason` is set |
| `outcome` | `acknowledged` \| `no_ack` \| `excluded` |
| `exclusion_reason` | `rejected_noise`, `rejected_non_user`, `no_transcript`, `not_addressed`, `speaker_muted`, `interrupted_by_user` |
| `failure_reason` | `dispatch_failed` — the command was valid and went **unserved** (the POST never landed). This is *not* an exclusion: the row stays eligible and counts against the KPI. A command os-server answered itself (local intent: volume, LED, time) is **not** a failure — its reply carries the interaction id as owner and counts as answered. |
| `ack_latency_ms` | Raw observation, kept whatever the verdict (`null` when nothing played) |
| `ack_modality`, `ack_kind` | What the user actually heard |
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
  **and** `observation_complete = true`. Rows with an incomplete observation
  are reported separately as coverage loss — never folded in as passes.
- Numerator: those with `stale_observed = true`.
- Report `explicit_stop` and `auto_supersede` separately: they are different
  policies. Automatic supersession only happens when os-server has
  `OS_REALTIME_SUPERSEDES_MAIN_REPLY=1`. **The code default is OFF and this
  change does not alter it** — but read the body's own `.env` before drawing
  conclusions: lamp ships with it **enabled**
  (`robots/lamp/rootfs/opt/hal/.env`), so lamp does produce `auto_supersede`
  samples. On a body where it is off the denominator is empty and that slice
  is **N/A**, not 100 %.
- Correct suppression (`stale_observed = false`) is a **pass**, not a missing
  response. Hardware actions of an auto-superseded turn remain valid by
  design; only speech and fillers are dropped.
- No eligible samples ⇒ **N/A**. Never report 0 % or 100 % from an empty set.

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
  -- A boundary with nothing to suppress is not a KPI situation; an
  -- incomplete observation is coverage loss, not a pass.
  COUNTIF(applicable > 0 AND complete = 'true')                    AS eligible_samples,
  COUNTIF(applicable > 0 AND complete = 'true' AND stale = 'true') AS stale_played,
  COUNTIF(applicable > 0 AND complete = 'false')                   AS incomplete_observations,
  CASE WHEN COUNTIF(applicable > 0 AND complete = 'true') = 0 THEN NULL
       ELSE ROUND(100 * COUNTIF(applicable > 0 AND complete = 'true' AND stale = 'true')
                      / COUNTIF(applicable > 0 AND complete = 'true'), 2)
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

## Local logs on the device

Every event is logged **before** it is sent, and delivery failures are logged
too — the warehouse copy is the one that can be missing.

```bash
journalctl -u hal -f | grep '\[tracking\]'        # HAL: every event + POST failures
journalctl -u hal -f | grep '\[voice-metrics\]'       # ack / boundary / stale decisions
journalctl -u os-server -f | grep '\[tracking\]'  # os-server: forwarded, dropped, failed
```

## Configuration

Both values live in the body's `/opt/hal/.env`, which os-server loads at
startup — a device can be pointed at a different warehouse without a rebuild:

| Key | Meaning |
|-----|---------|
| `AUTONOMOUS_TELEMETRY_ENABLED` | **Master switch. Default OFF** (empty/unset). Events are still written to the local log when off — only the network hop is skipped. Set to `1` to send. |
| `AUTONOMOUS_ANALYTICS_ID` | AA authorization key. Missing ⇒ delivery fails loudly (`[telemetry] delivery failed`). |
| `AUTONOMOUS_ANALYTICS_URL` | Where events are posted. Optional; falls back to the built-in production endpoint. |

The switch is read by both processes (HAL and os-server) from the same
key, per call — flipping it plus a service restart is enough, no rebuild.

Resolution order for the URL: process env → `/opt/hal/.env` → built-in
default. The HAL→os-server hop (`http://127.0.0.1:5000/api/telemetry/event`)
stays a loopback constant like every other HAL→OS call.

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
  `observation_complete = false` and must be reported as coverage loss.
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
