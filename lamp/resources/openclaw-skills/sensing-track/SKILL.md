---
name: sensing-track
description: Query flow event logs to answer questions about past sensing events — "Have you seen anybody between 10pm and 12pm?", "Is there any motion in the last hour?", "What happened while I was away?".
---

# Sensing Event History

## Quick Start

The primary data source is the **flow events JSONL** at `/root/local/flow_events_YYYY-MM-DD.jsonl`. Each file covers one calendar day (7-day retention, no size-rotation mid-day). Use Bash + `jq` to query it.

> **Important:** Always use the absolute path `/root/local/` — the `read` tool cannot access files outside the workspace, so use `exec` (Bash) for all JSONL queries.

Persistent camera snapshots are stored under `/var/lib/lelamp/snapshots/sensing_<prefix>/<ms>.jpg` (72h TTL, 50 MB cap) — one subdir per event kind:

| Event type | Folder |
|---|---|
| `presence.enter`, `presence.leave` | `sensing_face/` |
| `motion.activity` | `sensing_motion_activity/` |
| `emotion.detected` | `sensing_emotion/` |

Reference these when the user asks what happened visually.

## JSONL format

Each line is a JSON object:

```json
{"kind":"enter","node":"sensing_input","ts":1712345678.123,"seq":42,"trace_id":"run-abc","data":{"type":"presence.enter","message":"Person detected — 1 face(s) visible (friend (gray))\n[snapshot: /var/lib/lelamp/snapshots/sensing_face/1712345678123.jpg]"},"version":"1.2.3"}
{"kind":"exit","node":"sensing_input","ts":1712345678.456,"seq":43,"trace_id":"run-abc","duration_ms":332,"data":{"path":"agent","run_id":"run-abc"},"version":"1.2.3"}
```

Key fields:
- `node` — filter on `"sensing_input"` for sensing events
- `kind` — `"enter"` = event received, `"exit"` = event processed (with `duration_ms`)
- `data.type` — event type: `presence.enter`, `presence.leave`, `motion`, `motion.activity`, `sound`, `light.level`, `voice`, `voice_command`, `emotion.detected`, `speech_emotion.detected`
- `data.message` — natural-language description; may contain `[snapshot: /var/lib/lelamp/snapshots/sensing_<prefix>/<ms>.jpg]`
- `data.path` — in `exit` records: `"agent"` (forwarded), `"local"` (handled locally), or has `"error"` key (failed/dropped)
- `ts` — Unix timestamp (seconds with fractional ms)
- `trace_id` — correlates enter/exit and links to agent turn

## Tools

**Bash** — `jq`, `cat`, date arithmetic. No writes.

---

## Query recipes

### Timezone — always set before date arithmetic

```bash
export TZ=$(cat /etc/timezone)
```

### All sensing events in a time range

```bash
export TZ=$(cat /etc/timezone)
DATE="$(date +%Y-%m-%d)"
FROM_TS=$(date -d "$DATE 22:00:00" +%s)
TO_TS=$(date -d "$DATE 23:59:59" +%s)
jq -c 'select(.node=="sensing_input" and .kind=="enter" and .ts >= '"$FROM_TS"' and .ts <= '"$TO_TS"')' \
  "/root/local/flow_events_${DATE}.jsonl"
```

### Events of a specific type in the last N hours

Use `"motion"` for raw motion, `"motion.activity"` for activity analysis (LeLamp-categorised — bucket names `drink`/`break` and raw Kinetics sedentary labels like `using computer`, `writing`, `reading book`). Most queries want both:

```bash
export TZ=$(cat /etc/timezone)
SINCE=$(date -d "1 hour ago" +%s)
TODAY=$(date +%Y-%m-%d)
jq -c 'select(.node=="sensing_input" and .kind=="enter" and .ts >= '"$SINCE"' and (.data.type=="motion" or .data.type=="motion.activity"))' \
  "/root/local/flow_events_${TODAY}.jsonl"
```

### Any activity in the last N minutes

```bash
export TZ=$(cat /etc/timezone)
SINCE=$(date -d "30 minutes ago" +%s)
TODAY=$(date +%Y-%m-%d)
jq -c 'select(.node=="sensing_input" and .kind=="enter" and .ts >= '"$SINCE"')' \
  "/root/local/flow_events_${TODAY}.jsonl"
```

### Presence events only (who came by)

Names in messages are lowercase (`friend (gray)`). Use `test()` with `"i"` flag for case-insensitive search:

```bash
TODAY=$(date +%Y-%m-%d)
# All presence events
jq -c 'select(.node=="sensing_input" and .kind=="enter" and (.data.type=="presence.enter" or .data.type=="presence.leave"))' \
  "/root/local/flow_events_${TODAY}.jsonl"
# Search for a specific person (case-insensitive)
jq -c 'select(.node=="sensing_input" and .kind=="enter" and .data.type=="presence.enter" and (.data.message | test("gray";"i")))' \
  "/root/local/flow_events_${TODAY}.jsonl"
```

### Events spanning multiple days

```bash
export TZ=$(cat /etc/timezone)
YESTERDAY=$(date -d "yesterday" +%Y-%m-%d)
TODAY=$(date +%Y-%m-%d)
cat "/root/local/flow_events_${YESTERDAY}.jsonl" "/root/local/flow_events_${TODAY}.jsonl" \
  | jq -c 'select(.node=="sensing_input" and .kind=="enter" and .ts >= '"$FROM_TS"' and .ts <= '"$TO_TS"')'
```

### Dropped events (agent was busy)

```bash
TODAY=$(date +%Y-%m-%d)
jq -c 'select(.node=="sensing_input" and .kind=="exit" and .data.error != null)' \
  "/root/local/flow_events_${TODAY}.jsonl"
```

### List snapshots (72h TTL — older files may be purged)

Snapshots are bucketed into `sensing_face/` (presence), `sensing_motion_activity/`, `sensing_emotion/`. Recurse into subdirs:

```bash
# Most-recent snapshots across all categories
find /var/lib/lelamp/snapshots -type f -name '*.jpg' -printf '%T@ %p\n' | sort -rn | head -20 | cut -d' ' -f2-

# Only a specific category
ls -lt /var/lib/lelamp/snapshots/sensing_motion_activity/ | head -20
```

### Pose buckets (posture history)

Posture snapshots are NOT in `/var/lib/lelamp/snapshots/` — they live in tmp under a per-window bucket layout at `/tmp/lamp-sensing-snapshots/sensing_pose/buckets/<bucket_id>/`. A bucket only exists when a tumbling window closed with bad posture (`bad_ratio >= POSE_BAD_RATIO`). Kept buckets survive ~2 days (`POSE_BUCKET_KEEP_S`); windows that didn't fire a nudge are deleted immediately, so the buckets you can see are by definition "bad posture" sessions.

Each kept bucket contains:

- `<sample_ts>_<score>.jpg` — annotated frame per sample (skeleton overlay + RULA score)
- `bucket.json` — manifest:
  - `bucket_id`, `window_start_ts`, `window_end_ts`, `kept: true`
  - `summary` — same shape as the `[posture_summary:]` block on `motion.activity` (`bad_ratio`, `dominant_region`, `samples`, …)
  - `samples[]` — `{ts, score, risk_level, filename, left, right}` (per-side RULA body_scores + angles)
  - `worst_snapshots[]` — pre-selected worst filenames (the ones Lamp auto-attaches to `/dm` on posture nudges)

```bash
# List recent buckets (newest first)
ls -lt /tmp/lamp-sensing-snapshots/sensing_pose/buckets/ | head -10

# Read a specific bucket's manifest
jq . /tmp/lamp-sensing-snapshots/sensing_pose/buckets/1779259742/bucket.json

# Buckets that closed in the last 2 hours
find /tmp/lamp-sensing-snapshots/sensing_pose/buckets -maxdepth 1 -type d -mmin -120 -name '[0-9]*' | sort

# Worst-frame paths from the latest kept bucket
LATEST=$(ls -t /tmp/lamp-sensing-snapshots/sensing_pose/buckets/ | head -1)
jq -r '.worst_snapshots[]' "/tmp/lamp-sensing-snapshots/sensing_pose/buckets/${LATEST}/bucket.json" \
  | sed "s|^|/tmp/lamp-sensing-snapshots/sensing_pose/buckets/${LATEST}/|"

# Today's bad-posture sessions — bucket id == window_start unix-seconds
TODAY_START=$(date -d "today 00:00" +%s)
for b in /tmp/lamp-sensing-snapshots/sensing_pose/buckets/*/bucket.json; do
  jq --arg start "$TODAY_START" 'select((.window_start_ts | floor) >= ($start | tonumber)) | {bucket_id, dominant: .summary.dominant_region, bad_ratio: .summary.bad_ratio, started: .window_start_ts}' "$b"
done
```

Note: `motion.activity` event messages contain `[pose_bucket: <id>]` and `[pose_worst: <fn1>,<fn2>,...]` markers — parse these out of `data.message` when you need to map a sensing_input record to its bucket. Markers are present whenever a posture nudge folded in.

---

## Fallback: system log

For detailed debugging or when you need Go-side log context (errors, warnings, lifecycle details), fall back to `${LAMP_LOG:-/var/log/lamp.log}`:

```bash
LOG="${LAMP_LOG:-/var/log/lamp.log}"
sed 's/\x1b\[[0-9;]*m//g' "$LOG" | grep "sensing event received"
```

The system log uses lumberjack rotation (1 MB cap, 3 backups) — it may miss data during high traffic. Use it only when JSONL doesn't have enough detail, or when investigating bugs.

---

## Mood history

A dedicated mood history log tracks **user mood** per user. Only the user's emotional state is logged — not system events or lamp emotions. Each user's mood data lives in their own directory.

**Read API:**
```bash
# Current user's mood history (auto-detects who's present)
curl -s "http://127.0.0.1:5000/api/openclaw/mood-history?date=$(date +%Y-%m-%d)&last=100"

# Specific user's mood history
curl -s "http://127.0.0.1:5000/api/openclaw/mood-history?user=gray&date=$(date +%Y-%m-%d)&last=100"
```

**Write:** Follow the **Mood** skill to log user mood from camera or conversation.

```json
{"ts":1776138500,"seq":1,"hour":10,"mood":"happy","source":"camera","trigger":"laughing"}
{"ts":1776139200,"seq":2,"hour":10,"mood":"stressed","source":"conversation","trigger":"user said feeling overwhelmed"}
```

Storage: `/root/local/users/{name}/mood/YYYY-MM-DD.jsonl` (30-day retention).

---

## Rules

- **Never write to any log file** — they are owned by the system.
- **Answer conversationally** — translate results into natural language. Never dump raw JSON to the user.
- **Handle empty results** — if no matching events, say "I didn't detect any [type] events in that window."
- **Mention dropped events when relevant** — check `exit` records with `data.error` for events the agent missed. Mention it: "There was motion at 10:45 PM but I was mid-conversation and missed it."
- **Resolve relative times** — translate "last hour", "this morning", "while I was away" into concrete Unix timestamps using `date -d` before filtering.
- **Span multiple days** — for questions covering more than today, `cat` multiple JSONL files together.
- **Parse the message field** for who/what details — `friend (gray)`, `friend (chloe)`, `stranger (stranger_1)`, `Large movement detected`, etc.
- **Reference snapshots** — when the user asks "what did you see?", extract the `[snapshot: ...]` path from the message. Path format is `/var/lib/lelamp/snapshots/sensing_<prefix>/<ms>.jpg` (category subdir per event kind). Snapshots have 72h TTL — check the file exists before referencing (`test -f <path>`).
- **Posture history** — for questions about the user's posture ("how was I sitting this morning?", "show me my worst posture today"), scan `/tmp/lamp-sensing-snapshots/sensing_pose/buckets/`. Only sessions that crossed the bad-ratio threshold survive here, so the bucket list itself answers "when did my posture get bad today?". Read each bucket's `bucket.json` for `summary.dominant_region` and `summary.bad_ratio`, then reference `worst_snapshots[]` for representative frames.

---

## Examples

**Input:** "Have you seen anybody between 10pm and 12pm?"
**Action:** Query `data.type` in `["presence.enter"]` between 22:00 and 00:00 from today's JSONL.
**Response:** "Yes — I detected a stranger at 10:03 PM and again at 10:07 PM." or "No one came by between 10 PM and midnight."

---

**Input:** "Is there any motion in the last hour?"
**Action:** Query `data.type` in `["motion", "motion.activity"]` with `SINCE=$(date -d "1 hour ago" +%s)`.
**Response:** "Yes, I detected large movement 3 times — at 9:29, 9:59, and 10:12." or "No motion in the last hour."

---

**Input:** "What happened while I was away?"
**Action:** Ask the user when they left, or find the last `presence.leave` and query all events after that timestamp.
**Response:** "After around 3 PM — I saw motion at 4:30 PM and again at 5:15 PM. No one was identified though. I have snapshots from those moments if you want to see."

---

**Input:** "How was my posture today?"
**Action:** List today's pose buckets and aggregate `summary.dominant_region` + `summary.bad_ratio` from each `bucket.json`.
**Response:** "You had 3 bad-posture sessions today: a neck-flexion one at 10:14 AM (77% bad), another neck stretch at 1:39 PM (100% bad), and one trunk lean at 3:20 PM (62% bad). The worst frames are in the bucket dirs if you want me to pull one up."
