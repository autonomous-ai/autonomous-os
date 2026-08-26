package schedule

import (
	"log/slog"
	"strings"
	"time"
)

// ResolveTimezone resolves the wire's IANA zone name. Blank (never set yet) or
// unresolvable both fall back to UTC rather than failing the caller outright —
// a bad zone should degrade to UTC math, not drop every schedule.
func ResolveTimezone(name string) (*time.Location, error) {
	name = strings.TrimSpace(name)
	if name == "" {
		return time.UTC, nil
	}
	loc, err := time.LoadLocation(name)
	if err != nil {
		return time.UTC, err
	}
	return loc, nil
}

// SyncSchedules applies a full schedule.sync: persists the new schedule list
// together with the device-wide timezone in one atomic write, then
// recomputes+persists NextRunAt for every enabled schedule. Returns the
// per-schedule next-run map (only for schedules that actually produced one —
// disabled, manual, spent-once, or past-end_at schedules have nothing useful
// to report) so the caller (the MQTT handler's ack) can format it however the
// wire needs.
//
// Deliberately lives in this package rather than the MQTT handler, for two
// reasons: it doesn't need a live broker to test, and — more importantly — a
// test can seed a Runner with a REALISTIC (jittered) NextRunAt exactly the
// way production code produces one, instead of a hand-picked clean timestamp
// that can accidentally dodge bugs in the jitter/de-jitter math (see
// runner_test.go's CRITICAL-2 regression test).
func SyncSchedules(store *Store, schedules []Schedule, timezone, deviceID string, now time.Time) (applied int, nextRunAt map[string]time.Time, err error) {
	tz, tzErr := ResolveTimezone(timezone)
	if tzErr != nil {
		// Don't fail the whole sync over a bad tz string — the schedules still
		// land (with UTC math instead of localized math) rather than being
		// dropped entirely. Logged loudly since this needs a fix upstream.
		slog.Warn("schedule.sync: invalid timezone, falling back to UTC",
			"component", "schedule", "timezone", timezone, "error", tzErr)
	}

	if err := store.ReplaceWithTimezone(schedules, timezone); err != nil {
		return 0, nil, err
	}

	applied = len(schedules)
	nextRunAt = make(map[string]time.Time, applied)
	for _, sch := range schedules {
		if !sch.Enabled {
			continue
		}
		next, ok := sch.NextRun(now, tz, deviceID)
		if !ok {
			// A real, enabled schedule that computes to "never fires" is
			// exactly the "device silently ignores a real schedule" failure
			// mode the phase-5 brief warns about — surface it loudly even
			// though the sync as a whole still acks success. EXCEPT: manual
			// and a spent "once" resolve to "never fires" by design, every
			// single sync, for as long as they remain in the list — warning
			// on those would spam the log on completely healthy devices and
			// drown out the case this warning exists to catch (a genuinely
			// malformed daily/weekly/monthly/interval spec, or an end_at that
			// unexpectedly already lapsed).
			if !expectedNeverFires(sch, now) {
				slog.Warn("schedule.sync: schedule cannot be computed, skipping next_run_at",
					"component", "schedule", "id", sch.ID, "repeat", sch.Cadence.Repeat)
			}
			continue
		}
		nextRunAt[sch.ID] = next
		// Persist now so the runner's very next tick already agrees with what
		// was just acked, instead of recomputing (and potentially
		// re-jittering to a different instant) a minute later.
		if err := store.SetNextRun(sch.ID, next); err != nil {
			slog.Error("schedule.sync: persist next_run_at failed",
				"component", "schedule", "id", sch.ID, "error", err)
		}
	}
	return applied, nextRunAt, nil
}

// expectedNeverFires reports whether sch computing to "never fires again" is
// an everyday, by-design outcome rather than a sign of a malformed spec: a
// manual schedule always resolves this way (that IS its entire point — see
// RepeatManual), and a "once" whose At has already passed is simply spent,
// which is completely normal for as long as the backend keeps a fired "once"
// in the list. Both would otherwise re-warn on every single schedule.sync.
//
// Deliberately narrow: a past end_at on any OTHER cadence still warns, since
// that is exactly the kind of surprising/malformed-looking state the warning
// was added to surface.
func expectedNeverFires(sch Schedule, now time.Time) bool {
	switch sch.Cadence.Repeat {
	case RepeatManual:
		return true
	case RepeatOnce:
		return sch.Cadence.At != nil && !sch.Cadence.At.After(now)
	default:
		return false
	}
}
