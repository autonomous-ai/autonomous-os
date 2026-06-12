# Flow A — Build Patterns (full algorithm)

Load multi-day data and produce `patterns.json` for one user. Read this when invoked from `wellbeing/SKILL.md` Step 3b, or when the user explicitly asks about their habits.

## Self-throttle guard (run first, always)

```bash
PATTERNS=/root/local/users/{name}/habit/patterns.json
if [ -f "$PATTERNS" ] && [ $(( $(date +%s) - $(stat -c %Y "$PATTERNS") )) -lt 21600 ]; then
  cat "$PATTERNS"   # fresh < 6h — return existing, skip rebuild
  exit 0
fi
# Eligibility: ≥3 days from EITHER source (wellbeing or posture) is enough to
# rebuild the union pattern file. Each section internally skips if its source
# is too sparse.
WB=$(ls /root/local/users/{name}/wellbeing/*.jsonl 2>/dev/null | wc -l)
PO=$(ls /root/local/users/{name}/posture/*.jsonl 2>/dev/null | wc -l)
[ "$WB" -lt 3 ] && [ "$PO" -lt 3 ] && { echo "insufficient_data: wb=$WB po=$PO"; exit 0; }
```

This makes Flow A idempotent and safe to invoke from `wellbeing/SKILL.md` on every nudge. Cost is one `stat` + integer compare on the hot path; only the cold path (missing or stale patterns, ≥3 days of data) runs the full multi-day read below.

## Steps

1. **Load multi-day data**: read the last 14–30 days of wellbeing JSONL files for the user. The threshold for emitting patterns is still `days_observed ≥ 3`, so a new user gets early patterns from day 4; the wider window only deepens accuracy once data accumulates.
   ```bash
   ls /root/local/users/{name}/wellbeing/*.jsonl | sort | tail -30
   cat /root/local/users/{name}/wellbeing/YYYY-MM-DD.jsonl
   ```
   Track the total number of distinct dates loaded → `days_observed`.

2. **Filter relevant actions**. Only these can form habits:

   | Action | Habit type |
   |---|---|
   | `drink` | Hydration timing |
   | `break` | Rest timing |
   | `enter` | Arrival time |
   | `leave` | Departure time |
   | `using computer` | Work session start |
   | `writing`, `texting`, `reading book`, `drawing` | Activity patterns |
   | Raw eat labels (`eating burger`, `eating cake`, `eating chips`, `eating doughnuts`, `eating hotdog`, `eating ice cream`, `eating spaghetti`, `eating watermelon`, `eating carrots`, `dining`, `tasting food`) | Meal timing — **collapse all to a single `eat` bucket** (typical_hour computed across the group, food-specific subhabits are overkill for v1) |
   | `coffee` | Coffee timing (from conversation intent) |
   | `sleep` | Sleep timing (from conversation intent) |
   | `exercise` | Exercise timing (from conversation intent) |

   Skip: `nudge_hydration`, `nudge_break`, `morning_greeting`, `sleep_winddown`, `meal_reminder`, `emotional` — these are agent-written nudges/reminders, not user activities, and would pollute pattern detection.

3. **Group by (action, hour)**. For each pair, collect the list of dates it appeared:
   ```
   drink @ hour=9  → [2026-04-15, 2026-04-17, 2026-04-18, 2026-04-20, 2026-04-21]
   drink @ hour=14 → [2026-04-15, 2026-04-16, 2026-04-20]
   enter @ hour=8  → [2026-04-15, 2026-04-16, 2026-04-17, 2026-04-18, 2026-04-19, 2026-04-20, 2026-04-21]
   ```

4. **Compute frequency**: `frequency = len(dates_appeared) / days_observed`

5. **Compute typical minute**. For days where the action occurred at the habitual hour, collect the minute values and take the median:
   ```
   drink @ hour=9 minutes: [08, 14, 22, 07, 10] → median = 10 → typical_minute = 10
   ```

6. **Assign strength** per the table in `SKILL.md`.

7. **Build habit objects** and write to `patterns.json`:
   ```bash
   mkdir -p /root/local/users/{name}/habit
   cat > /root/local/users/{name}/habit/patterns.json << 'PATTERNS'
   {the computed JSON}
   PATTERNS
   ```

## patterns.json schema

```json
{
  "updated_at": "2026-04-22T08:00:00",
  "days_observed": 7,
  "wellbeing_patterns": [
    {
      "action": "drink",
      "typical_hour": 9,
      "typical_minute": 10,
      "window_minutes": 30,
      "frequency": 0.71,
      "days_observed": 7,
      "strength": "moderate"
    },
    {
      "action": "enter",
      "typical_hour": 8,
      "typical_minute": 30,
      "window_minutes": 45,
      "frequency": 1.0,
      "days_observed": 7,
      "strength": "strong"
    }
  ],
  "music_patterns": [
    {
      "preferred_genre": "lo-fi",
      "peak_hour": 14,
      "acceptance_rate": 0.8,
      "days_observed": 5
    }
  ],
  "posture_patterns": [
    {
      "peak_hour": 15,
      "side_bias": "right",
      "typical_risk_bucket": "medium",
      "top_offenders": ["neck", "right_arm"],
      "alerts_per_day": 6.0,
      "days_observed": 7,
      "strength": "strong"
    }
  ]
}
```

## Posture patterns (Flow A — posture extension)

Triggered from `posture/SKILL.md` when its context block sets `bootstrap_needed=true` on a nudge turn. Computes one `posture_patterns` entry per user (single object — posture has no per-action sub-categories like wellbeing).

Inputs: `/root/local/users/{name}/posture/*.jsonl` rows where `action == "posture_alert"` (alert rows are the only ones with ergonomic-risk facts; nudge/praise rows are agent output).

Steps:

1. **Load multi-day data.** Read up to last 14 daily files. `days_observed` = number of distinct dates with at least one alert row.
2. **Skip if too sparse.** Require ≥3 days with alerts AND ≥6 total alert rows. Otherwise emit `posture_patterns: []` (insufficient data — no pattern).
3. **peak_hour** = the hour 0..23 with the most alert rows across the window. Ties → earlier hour.
4. **side_bias** = compare summed `left_score` vs summed `right_score` across all alerts. Diff ≥ 1.5× the smaller side → name the dominant side; otherwise `"none"`.
5. **typical_risk_bucket** = mode of `risk` field (`"medium"` vs `"high"`).
6. **top_offenders** (best-effort): not available from row schema (alert rows store only `score`, `risk`, `left_score`, `right_score` — sub-region scores live in the original event message, not persisted). Leave as `[]` until the backend starts logging top offenders. Field exists for forward compatibility.
7. **alerts_per_day** = round(total_alerts / days_observed, 1).
8. **strength**: same table as wellbeing. Use `(days_with_alerts / days_observed)` as the frequency analog. `< 0.50` → skip pattern; `0.50–0.75` → moderate; `> 0.75` → strong.

Skip non-alert posture rows (`nudge_posture`, `praise_posture`) — those are agent reactions, not user signal.
