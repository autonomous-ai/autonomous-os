// Package schedule is the on-device half of the "Scheduled" feature: it
// stores the recurring-task list the backend pushes over MQTT (schedule.sync),
// works out when each task is next due, fires it through whichever agentic
// runtime is currently active, and reports the outcome back.
//
// Deliberately lives here — in os-server — rather than inside any runtime
// (openclaw/hermes/claudecode/codex/picoclaw/opencode): a device owner can
// switch runtimes at any time, and all six implement the two methods this
// package needs, domain.AgentGateway.SendSystemChatMessage (for an "agent"
// task) and domain.AgentGateway.Speak (for a "speak" one). Putting the
// scheduler in a runtime (e.g. riding OpenClaw's own cron subsystem) would
// strand every schedule the moment the owner switched away from OpenClaw.
package schedule

import (
	"hash/fnv"
	"log/slog"
	"strconv"
	"strings"
	"time"
)

// Repeat cadence values — exact strings per the MQTT schedule.sync wire
// contract (phase-5 brief). Do not rename: the backend and the web app both
// send/expect these verbatim.
const (
	RepeatDaily    = "daily"
	RepeatWeekly   = "weekly"
	RepeatMonthly  = "monthly"
	RepeatInterval = "interval"
	RepeatOnce     = "once"
	RepeatManual   = "manual"
)

// maxJitter bounds the deterministic per-device offset NextRunForDevice folds
// into daily/weekly/monthly occurrences (see JitterOffset). ±5 minutes per the
// phase-5 brief's ambiguity resolution: enough to spread a fleet of devices
// that all share the exact same cadence (e.g. everyone's "8am daily briefing")
// across a 10-minute window instead of every device hitting the backend/MQTT
// broker in the same instant.
const maxJitter = 5 * time.Minute

// Spec is the cadence half of a schedule — the wire's nested "schedule"
// object, e.g. {"repeat":"weekly","days":[1,2,3,4,5],"time":"08:00"}. Only the
// fields relevant to Repeat are populated; the rest are left zero and ignored.
//
// EndAt is deliberately a field here even though the wire's end_at is a
// SIBLING of "schedule", not nested inside it (see Schedule in store.go). This
// keeps Spec.NextRun fully self-contained and directly testable/usable on its
// own — exactly how the phase-5 brief's own example constructs a bare
// Spec{Repeat: "manual"} and calls NextRun on it. Schedule.NextRun (store.go)
// copies its own top-level EndAt into a working Spec value before delegating,
// so the wire shape is respected end to end.
type Spec struct {
	Repeat string `json:"repeat"`

	// Days lists weekdays for "weekly": 0=Sunday..6=Saturday — matching Go's
	// own time.Weekday AND the backend's tagged proto (the canonical shape per
	// controller ruling after review). 7 is additionally accepted as an alias
	// for Sunday, for robustness against an off-by-one payload. The brief's
	// own example days:[1,2,3,4,5] means Mon-Fri under BOTH this convention
	// and ISO-8601 — they only disagree on Sunday (0 vs 7), which is exactly
	// why that example could not disambiguate the convention on its own.
	Days []int `json:"days,omitempty"`

	// DayOfMonth is the target day for "monthly", 1-31. A short month clamps to
	// its actual last day (e.g. day_of_month=31 in February -> the 28th, 29th
	// in a leap year) rather than rolling over into the next month.
	DayOfMonth int `json:"day_of_month,omitempty"`

	// Time is the wall-clock fire time "HH:MM" (24h), evaluated in the tz
	// passed to NextRun. Used by daily/weekly/monthly.
	Time string `json:"time,omitempty"`

	// EveryMs is the gap for "interval" schedules, in MILLISECONDS — the
	// backend sends milliseconds (canonical shape per controller ruling after
	// review; an earlier revision of this field guessed seconds, a 1000x
	// error). Unlike the other cadences this is duration-anchored by
	// definition (next = last run + interval), not a recurring wall-clock
	// point, so it is deliberately exempt from both the time.Date/DST handling
	// below and from jitter.
	//
	// nextInterval floors this at minInterval: the device is the last line of
	// defence against a misconfigured (or malicious) near-zero interval, since
	// SendSystemChatMessage has no rate limiting of its own anywhere in the
	// send path (see the phase-5 report's rate-limiting finding).
	EveryMs uint64 `json:"every_ms,omitempty"`

	// At is the absolute fire time for "once". Never jittered (see
	// NextRunForDevice) — a user who picked an exact time does not expect it
	// to move.
	At *time.Time `json:"at,omitempty"`

	// EndAt: see the type doc comment above. json:"-" keeps it out of Spec's
	// own encoding so it is never double-stored alongside Schedule's real
	// on-the-wire/on-disk end_at.
	EndAt *time.Time `json:"-"`
}

// NextRun returns the earliest occurrence of s strictly after `after`, in the
// wall clock of tz. ok=false means "never fires again": a manual schedule, a
// "once" whose At has already passed, or a schedule at/past its EndAt.
//
// All wall-clock arithmetic goes through time.Date so the stdlib — not manual
// offset math — resolves DST gaps and overlaps. Interval and once are the two
// exceptions: they are duration/absolute-anchored by definition rather than
// recurring wall-clock points, so they use plain time arithmetic instead (see
// nextInterval/nextOnce).
func (s Spec) NextRun(after time.Time, tz *time.Location) (time.Time, bool) {
	if s.EndAt != nil && !after.Before(*s.EndAt) {
		return time.Time{}, false
	}

	next, ok := s.nextOccurrence(after, tz)
	if !ok {
		return time.Time{}, false
	}
	if s.EndAt != nil && next.After(*s.EndAt) {
		return time.Time{}, false
	}
	return next, true
}

func (s Spec) nextOccurrence(after time.Time, tz *time.Location) (time.Time, bool) {
	switch s.Repeat {
	case RepeatDaily:
		return s.nextDaily(after, tz)
	case RepeatWeekly:
		return s.nextWeekly(after, tz)
	case RepeatMonthly:
		return s.nextMonthly(after, tz)
	case RepeatInterval:
		return s.nextInterval(after)
	case RepeatOnce:
		return s.nextOnce(after)
	case RepeatManual:
		return time.Time{}, false
	default:
		// Unknown repeat value: fail closed (never fires) rather than guessing.
		return time.Time{}, false
	}
}

// parseTime parses "HH:MM" into hour/minute. A malformed or missing time makes
// the schedule un-computable (ok=false) rather than panicking.
func parseTime(hhmm string) (hour, minute int, ok bool) {
	parts := strings.SplitN(hhmm, ":", 2)
	if len(parts) != 2 {
		return 0, 0, false
	}
	h, err1 := strconv.Atoi(parts[0])
	m, err2 := strconv.Atoi(parts[1])
	if err1 != nil || err2 != nil || h < 0 || h > 23 || m < 0 || m > 59 {
		return 0, 0, false
	}
	return h, m, true
}

// nextDaily finds the next HH:MM occurrence strictly after `after`. Candidates
// are always built via time.Date for the exact calendar day (never via
// Add(24*time.Hour), which would drift by an hour across a DST transition) —
// this is also what makes a nonexistent (spring-forward gap) or repeated
// (fall-back overlap) wall-clock time resolve to exactly ONE instant per
// calendar day: the stdlib normalizes it, we never scan through both
// candidate offsets ourselves.
func (s Spec) nextDaily(after time.Time, tz *time.Location) (time.Time, bool) {
	h, m, ok := parseTime(s.Time)
	if !ok {
		return time.Time{}, false
	}
	local := after.In(tz)
	// dayOffset=1 (tomorrow at HH:MM) is always strictly after `after`, so this
	// loop always terminates by then; the explicit bound just avoids an
	// unbounded loop rather than relying on that always being true.
	for dayOffset := 0; dayOffset <= 1; dayOffset++ {
		candidate := time.Date(local.Year(), local.Month(), local.Day()+dayOffset, h, m, 0, 0, tz)
		if candidate.After(after) {
			return candidate, true
		}
	}
	return time.Time{}, false
}

// normalizeWeekday maps a wire `days` value to time.Weekday. The canonical
// convention (matching Go's own time.Weekday AND the backend's tagged proto)
// is 0=Sunday..6=Saturday; 7 is additionally accepted as an alias for Sunday
// for robustness against an off-by-one payload.
func normalizeWeekday(d int) time.Weekday {
	if d == 7 {
		return time.Sunday
	}
	return time.Weekday(d)
}

// nextWeekly scans forward day by day (via time.Date, never duration Add) for
// the next listed weekday at HH:MM. A full week of slack (dayOffset up to 7)
// guarantees a hit even when today's own slot has already passed.
func (s Spec) nextWeekly(after time.Time, tz *time.Location) (time.Time, bool) {
	if len(s.Days) == 0 {
		return time.Time{}, false
	}
	h, m, ok := parseTime(s.Time)
	if !ok {
		return time.Time{}, false
	}
	wanted := make(map[time.Weekday]bool, len(s.Days))
	for _, d := range s.Days {
		wanted[normalizeWeekday(d)] = true
	}
	local := after.In(tz)
	for dayOffset := 0; dayOffset <= 7; dayOffset++ {
		candidate := time.Date(local.Year(), local.Month(), local.Day()+dayOffset, h, m, 0, 0, tz)
		if !wanted[candidate.Weekday()] {
			continue
		}
		if candidate.After(after) {
			return candidate, true
		}
	}
	return time.Time{}, false
}

// lastDayOfMonth returns how many days `month` has in `year`. Day 0 of the
// following month is, by definition, the last day of this one — a standard Go
// idiom that naturally accounts for leap Februaries.
func lastDayOfMonth(year int, month time.Month) int {
	return time.Date(year, month+1, 0, 0, 0, 0, 0, time.UTC).Day()
}

// nextMonthly finds the next DayOfMonth (clamped to the month's real last day)
// at HH:MM. 13 iterations cover this month through a full year out — more
// headroom than any realistic schedule needs, but bounded rather than open-ended.
func (s Spec) nextMonthly(after time.Time, tz *time.Location) (time.Time, bool) {
	if s.DayOfMonth < 1 || s.DayOfMonth > 31 {
		return time.Time{}, false
	}
	h, m, ok := parseTime(s.Time)
	if !ok {
		return time.Time{}, false
	}
	local := after.In(tz)
	year, month := local.Year(), local.Month()
	for i := 0; i < 13; i++ {
		total := int(month) - 1 + i
		y := year + total/12
		mo := time.Month(total%12 + 1)
		day := s.DayOfMonth
		if last := lastDayOfMonth(y, mo); day > last {
			day = last // clamp: day_of_month=31 in February -> the 28th/29th
		}
		candidate := time.Date(y, mo, day, h, m, 0, 0, tz)
		if candidate.After(after) {
			return candidate, true
		}
	}
	return time.Time{}, false
}

// minInterval floors "interval" schedules. The device is the LAST line of
// defence here: SendSystemChatMessage is a fire-and-forget call straight into
// the active runtime with no rate limiting anywhere in that path (see the
// phase-5 report's rate-limiting finding), so a misconfigured or malicious
// tiny every_ms would otherwise spam paid LLM turns as fast as the 1-minute
// runner ticker allows.
const minInterval = 5 * time.Minute

// nextInterval anchors on `after` (the last run, per the caller) rather than
// on wall-clock boundaries — an interval schedule means "this long after it
// last ran", full stop.
func (s Spec) nextInterval(after time.Time) (time.Time, bool) {
	if s.EveryMs == 0 {
		return time.Time{}, false
	}
	interval := time.Duration(s.EveryMs) * time.Millisecond
	if interval < minInterval {
		slog.Warn("schedule: interval below the floor, clamping",
			"component", "schedule", "requested", interval, "floor", minInterval)
		interval = minInterval
	}
	return after.Add(interval), true
}

// nextOnce fires exactly once, at At, and never again once At has passed.
func (s Spec) nextOnce(after time.Time) (time.Time, bool) {
	if s.At == nil || !s.At.After(after) {
		return time.Time{}, false
	}
	return *s.At, true
}

// JitterOffset deterministically maps (deviceID, scheduleID) to an offset in
// [-maxJitter, +maxJitter). FNV-1a rather than a cryptographic hash: this is a
// thundering-herd guard, not a security boundary — every device computing the
// same two ids must derive the exact same offset, forever, with no shared
// state or coordination (see NewRunner's deviceID parameter: this function
// never reaches into global/device config itself).
//
// Hashes into whole SECONDS, not nanoseconds. fnv32a's Sum32 is a uint32 —
// its entire range (~4.29e9) is smaller than 2*maxJitter expressed in
// nanoseconds (600e9), so `int64(sum) % spanNanos` was a silent no-op and this
// function used to return a constant ~-5m for every input (CRITICAL finding
// from the phase-5 review). Reducing the span to whole seconds (600) before
// the modulo actually exercises the hash's range.
func JitterOffset(deviceID, scheduleID string) time.Duration {
	h := fnv.New32a()
	_, _ = h.Write([]byte(deviceID + "\x00" + scheduleID))
	sum := h.Sum32()
	spanSeconds := int64(2 * maxJitter / time.Second)
	secs := int64(sum) % spanSeconds
	return time.Duration(secs)*time.Second - maxJitter
}

// jitteredRepeat reports whether repeat gets NextRunForDevice's deterministic
// jitter. Extracted into its own function (rather than duplicating the
// switch) so NextRunForDevice and DejitterAnchor can never drift on which
// cadences are jittered — a drift here is exactly how CRITICAL-2 from the
// phase-5 review happened (fire() didn't know it had to reverse the jitter
// before feeding a stored occurrence back into NextRun).
func jitteredRepeat(repeat string) bool {
	switch repeat {
	case RepeatDaily, RepeatWeekly, RepeatMonthly:
		return true
	default:
		return false
	}
}

// NextRunForDevice is Spec.NextRun plus the deterministic jitter, applied only
// to the wall-clock-recurring cadences (daily/weekly/monthly). Interval and
// once are exempt: an interval schedule is already anchored to a real instant
// (jittering it would make "every 30s" drift every cycle), and a "once" fires
// at the exact instant the user picked.
func NextRunForDevice(spec Spec, after time.Time, tz *time.Location, deviceID, scheduleID string) (time.Time, bool) {
	next, ok := spec.NextRun(after, tz)
	if !ok {
		return time.Time{}, false
	}
	if jitteredRepeat(spec.Repeat) {
		next = next.Add(JitterOffset(deviceID, scheduleID))
	}
	return next, true
}

// DejitterAnchor reverses the jitter NextRunForDevice may have applied to a
// stored occurrence, returning the TRUE (unjittered) instant. Required
// whenever a jittered NextRunAt is fed back in as the `after` cursor to
// compute the FOLLOWING occurrence: because jitter can shift an occurrence
// EARLIER than its true wall-clock boundary, passing the jittered value
// straight back into NextRun/NextRunForDevice can land `after` still inside
// the very cadence period it was itself part of — so NextRun returns the SAME
// occurrence again and the schedule never advances.
//
// This was CRITICAL-2 from the phase-5 review: runner.fire() called
// sch.NextRun(sch.NextRunAt, ...) directly, so any schedule whose jitter
// happened to be negative re-fired on every 1-minute tick until it aged past
// the 30-minute catch-up window.
func DejitterAnchor(repeat string, jitteredAt time.Time, deviceID, scheduleID string) time.Time {
	if !jitteredRepeat(repeat) {
		return jitteredAt
	}
	return jitteredAt.Add(-JitterOffset(deviceID, scheduleID))
}
