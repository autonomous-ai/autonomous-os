# Voice KPI tracking

Measurement only. Nothing in this document changes what the device says, when
it speaks, or when it stays quiet — it records what already happened so two
numbers can be computed from real devices:

| KPI | Question | Target |
|-----|----------|--------|
| **KPI-1** | Share of eligible voice interactions acknowledged within **3 s** of the detected end of the user's speech | ≥ 95 % |
| **KPI-2** | Share of eligible suppression boundaries where an **outdated** reply was actually played | < 1 % |

## Where the code lives

| Layer | Path | Role |
|-------|------|------|
| HAL tracker | `hal/tracking/voice_kpi.py` | All measurement. Owns interaction ids, ack detection, stale-playback detection. |
| HAL pipe | `hal/tracking/client.py` | Generic: local log line, bounded queue, background POST. Reusable by future trackers. |
| OS ingestion | `system/server/tracking/delivery/http/handler.go` | `POST /api/tracking/event` (loopback/LAN, same gate as `/api/sensing/event`). |
| OS pipe | `system/tracking/tracking.go` | Common fields, de-duplication, bounded queue, one sender, local log line. |
| Transport | `system/lib/analytics` | Autonomous Analytics `event_tracking` client (shared with web/mobile). |

Voice code is touched only at the boundaries it already owns: speech endpoint,
turn dispatch, TTS playback start/end, and the cancel gesture. The rules,
thresholds and event shapes all live in the two tracking packages.

## Interaction id

An interaction is **one user utterance**. `voice_kpi.speech_end()` creates
`interaction_id` (`vi-<16 hex>`) at the moment HAL decides the user stopped
speaking, and it is carried through realtime, the sensing POST (bound to the
os-server `runId` returned by `/api/sensing/event`), the main agent's reply
(via the TTS queue `turn_id`), fillers and playback.

`voice_agent_handled` is a **backend notification about an interaction that
was already answered**, not a new one: it binds to the same `interaction_id`,
so a realtime-handled turn produces exactly one KPI-1 sample.

## What counts as an acknowledgement

The first **audio frame** out of the speaker — `TTSService`'s `on_speak_start`
hook, which fires on first frame, not on HTTP acceptance. `speak_queue()`
deliberately returns `True` for requests it drops (superseded turn), and
os-server's `deliverTTS` can be muted after accepting text, so neither
acceptance, queued text, model output, nor `tts_send` proves the user heard
anything.

| Playback kind | Modality recorded | How it is classified |
|---------------|-------------------|----------------------|
| `native_realtime` | `spoken_answer_realtime` | `TTSService.native_mode` — realtime model's own voice |
| `agent_reply` | `spoken_answer` | `realtime_feedback` (only the agent's reply sets it), carries `latest_queue_turn_id` |
| `waiting_audio` | `waiting_audio` | interruptible cached speech, i.e. a dead-air filler |
| `system_audio` | `acknowledgement_audio` | any other cached/system phrase |

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
`music_started`, `max_duration`, `stt_error`.

## Events

### `voice_kpi_interaction` — one per utterance (KPI-1)

| Field | Meaning |
|-------|---------|
| `interaction_id`, `run_id`, `event_type`, `route` | Identity + how the turn was routed (`handled` / `delegated` / fallback / noise-dropped) |
| `speech_end_method` | How the endpoint was detected |
| `eligible` | `false` when `exclusion_reason` is set |
| `outcome` | `acknowledged` \| `no_ack` \| `excluded` |
| `exclusion_reason` | `rejected_noise`, `rejected_non_user`, `no_transcript`, `not_addressed`, `speaker_muted`, `interrupted_by_user` |
| `ack_latency_ms` | Raw observation, kept whatever the verdict (`null` when nothing played) |
| `ack_modality`, `ack_kind` | What the user actually heard |
| `ack_deadline_ms`, `observe_window_ms` | 3000 / 10000 — the thresholds in force when the row was written |

The observation window is **10 s**, wider than the 3 s target on purpose: a
late answer is reported with its real latency instead of collapsing into "no
answer at all", so the threshold can be re-decided from stored data.

### `voice_kpi_suppression` — one per boundary (KPI-2)

| Field | Meaning |
|-------|---------|
| `suppression_reason` | `explicit_stop` (user clicked) or `auto_supersede` (realtime answered a newer utterance) |
| `interaction_id` | The utterance that triggered the boundary |
| `applicable_interactions` | How many older in-flight interactions the boundary covers |
| `old_audio_playing_at_boundary` | Whether old audio was already playing when the boundary was stamped |
| `stale_observed` | **KPI-2 numerator** |
| `stale_kind`, `stale_started_after_ms` | Which audio, and how long after the boundary it started |
| `stop_to_silence_ms` | How long old audio kept playing after the boundary — recorded raw, even inside the grace |
| `grace_ms`, `observe_window_ms` | 2000 / 3000 |

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

## KPI definitions

**KPI-1 — acknowledged within 3 s**

- Denominator: `voice_kpi_interaction` rows with `eligible = true`.
- Numerator: those with `outcome = 'acknowledged'` and `ack_latency_ms <= 3000`.
- Excluded (reported, never dropped): noise-rejected turns, turns the realtime
  model explicitly rejected as non-user, empty transcripts, utterances not
  addressed to the device (no wake word / outside the follow-up window), and a
  muted speaker. Slow and failed eligible interactions **stay in the
  denominator**.

**KPI-2 — outdated reply actually played**

- Denominator: `voice_kpi_suppression` rows.
- Numerator: those with `stale_observed = true`.
- Report `explicit_stop` and `auto_supersede` separately: they are different
  policies. Automatic supersession only happens when os-server has
  `OS_REALTIME_SUPERSEDES_MAIN_REPLY=1`; **the default is OFF and this change
  does not alter it**, so on a default body the `auto_supersede` denominator
  will be empty and that KPI slice is **N/A**, not 100 %.
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
WITH i AS (
  SELECT
    param(data.event_params, 'eligible')       AS eligible,
    param(data.event_params, 'outcome')        AS outcome,
    SAFE_CAST(param(data.event_params, 'ack_latency_ms') AS INT64) AS ack_ms
  FROM event_tracking
  WHERE event_name = 'voice_kpi_interaction'
    AND event_timestamp >= UNIX_SECONDS(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY))
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
    param(data.event_params, 'suppression_reason') AS reason,
    param(data.event_params, 'stale_observed')     AS stale
  FROM event_tracking
  WHERE event_name = 'voice_kpi_suppression'
    AND event_timestamp >= UNIX_SECONDS(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY))
)
SELECT
  reason,
  COUNT(*) AS eligible_samples,
  COUNTIF(stale = 'true') AS stale_played,
  CASE WHEN COUNT(*) = 0 THEN NULL
       ELSE ROUND(100 * COUNTIF(stale = 'true') / COUNT(*), 2)
  END AS kpi2_pct
FROM s
GROUP BY reason;
```

```sql
-- Telemetry coverage: a climbing loss counter means the rates above are
-- computed on partial data.
SELECT
  MAX(SAFE_CAST(param(data.event_params, 'tracking_dropped_total') AS INT64)) AS os_dropped,
  MAX(SAFE_CAST(param(data.event_params, 'tracking_failed_total')  AS INT64)) AS os_failed,
  MAX(SAFE_CAST(param(data.event_params, 'hal_dropped_total')      AS INT64)) AS hal_dropped,
  MAX(SAFE_CAST(param(data.event_params, 'hal_failed_total')       AS INT64)) AS hal_failed
FROM event_tracking
WHERE event_name LIKE 'voice_kpi_%';
```

## Local logs on the device

Every event is logged **before** it is sent, and delivery failures are logged
too — the warehouse copy is the one that can be missing.

```bash
journalctl -u hal -f | grep '\[tracking\]'        # HAL: every event + POST failures
journalctl -u hal -f | grep '\[voice-kpi\]'       # ack / boundary / stale decisions
journalctl -u os-server -f | grep '\[tracking\]'  # os-server: forwarded, dropped, failed
```

## Privacy

Transcripts, reply text, raw audio and credentials are never sent. Only ids,
enumerated outcomes, and durations. `user_pseudo_id` is the device hostname
and `platform` is `device` (see `system/lib/analytics`).

## Limitations

- **Detected endpoint, not acoustic end.** Latency is measured from the
  silence clock / STT final, not from the physical end of speech.
- **Visual acknowledgement is not measured** (see above).
- **`auto_supersede` is empty on a default body** — `OS_REALTIME_SUPERSEDES_MAIN_REPLY`
  defaults to OFF and was not changed.
- **Stale detection is playback-level, not sample-level.** It observes audio
  starting or still running after a boundary; it cannot say how many
  milliseconds of old audio were audible beyond `stop_to_silence_ms`.
- **Attribution for fillers and realtime voice is by newest open interaction**,
  not an explicit id — those paths carry no turn id. Agent replies are
  attributed exactly, by run id.
- **In-memory state only.** A HAL restart loses interactions still in their
  observation window; those samples are missing rather than wrong.
- **Delivery loss is reported, not hidden**: `tracking_dropped_total`,
  `tracking_failed_total`, `hal_dropped_total`, `hal_failed_total` ride on
  every event.

## Validation

```bash
go build ./...                                   # os-server + tracking package
go test ./system/tracking/ ./system/server/tracking/...
make hal-lint
cd hal && .venv/bin/python -m pytest test/test_voice_kpi.py -q
```
