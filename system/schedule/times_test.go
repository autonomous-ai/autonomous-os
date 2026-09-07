package schedule

import (
	"testing"
	"time"
)

func mustTZ(t *testing.T) *time.Location {
	t.Helper()
	tz, err := time.LoadLocation("Asia/Ho_Chi_Minh")
	if err != nil {
		t.Fatalf("load tz: %v", err)
	}
	return tz
}

// A multi-time daily must walk its times in order across the day and then roll
// to the first time tomorrow — not fire the earliest time repeatedly.
func TestNextDaily_WalksEveryTime(t *testing.T) {
	tz := mustTZ(t)
	spec := Spec{Repeat: RepeatDaily, Times: []string{"09:00", "13:00", "17:00"}}

	at := func(h, m int) time.Time { return time.Date(2026, 8, 29, h, m, 0, 0, tz) }

	for _, tc := range []struct {
		after time.Time
		want  time.Time
	}{
		{at(8, 0), at(9, 0)},
		{at(9, 0), at(13, 0)},   // strictly after: the 09:00 slot is spent
		{at(12, 59), at(13, 0)},
		{at(13, 1), at(17, 0)},
		{at(17, 1), time.Date(2026, 8, 30, 9, 0, 0, 0, tz)}, // rolls to tomorrow's first
	} {
		got, ok := spec.NextRun(tc.after, tz)
		if !ok {
			t.Fatalf("after %v: no next run", tc.after)
		}
		if !got.Equal(tc.want) {
			t.Fatalf("after %v: got %v, want %v", tc.after, got, tc.want)
		}
	}
}

// The compatibility contract: a schedule stored before Times existed carries
// only Time, and must behave exactly as it did.
func TestNextDaily_PreTimesSpecUnchanged(t *testing.T) {
	tz := mustTZ(t)
	spec := Spec{Repeat: RepeatDaily, Time: "09:00"}

	got, ok := spec.NextRun(time.Date(2026, 8, 29, 10, 0, 0, 0, tz), tz)
	if !ok {
		t.Fatal("no next run")
	}
	want := time.Date(2026, 8, 30, 9, 0, 0, 0, tz)
	if !got.Equal(want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}

// Times wins when both are present — the backend keeps Time == Times[0], but a
// disagreeing payload must not produce a fire the user never asked for.
func TestNextDaily_TimesOverridesTime(t *testing.T) {
	tz := mustTZ(t)
	spec := Spec{Repeat: RepeatDaily, Time: "23:00", Times: []string{"09:00"}}

	got, _ := spec.NextRun(time.Date(2026, 8, 29, 8, 0, 0, 0, tz), tz)
	want := time.Date(2026, 8, 29, 9, 0, 0, 0, tz)
	if !got.Equal(want) {
		t.Fatalf("got %v, want %v — Times must win over a stale Time", got, want)
	}
}

// Weekly is the cross product of days x times, so both slots on a listed day
// fire before the schedule moves to the next day.
func TestNextWeekly_CrossProductOfDaysAndTimes(t *testing.T) {
	tz := mustTZ(t)
	// Saturday 2026-08-29. Days = Mon(1), Wed(3).
	spec := Spec{Repeat: RepeatWeekly, Days: []int{1, 3}, Times: []string{"09:00", "17:00"}}

	// From Saturday, the next slot is Monday 09:00.
	got, ok := spec.NextRun(time.Date(2026, 8, 29, 12, 0, 0, 0, tz), tz)
	if !ok {
		t.Fatal("no next run")
	}
	mon := time.Date(2026, 8, 31, 9, 0, 0, 0, tz)
	if !got.Equal(mon) {
		t.Fatalf("got %v, want Monday 09:00 %v", got, mon)
	}
	if got.Weekday() != time.Monday {
		t.Fatalf("got weekday %v, want Monday", got.Weekday())
	}

	// Monday 09:00 -> Monday 17:00, not straight to Wednesday.
	got, _ = spec.NextRun(mon, tz)
	want := time.Date(2026, 8, 31, 17, 0, 0, 0, tz)
	if !got.Equal(want) {
		t.Fatalf("got %v, want same-day 17:00 %v", got, want)
	}

	// Monday 17:00 -> Wednesday 09:00.
	got, _ = spec.NextRun(want, tz)
	wed := time.Date(2026, 9, 2, 9, 0, 0, 0, tz)
	if !got.Equal(wed) {
		t.Fatalf("got %v, want Wednesday 09:00 %v", got, wed)
	}
}

// Monthly keeps its short-month clamp while walking multiple times.
func TestNextMonthly_MultipleTimesKeepsClamp(t *testing.T) {
	tz := mustTZ(t)
	spec := Spec{Repeat: RepeatMonthly, DayOfMonth: 31, Times: []string{"09:00", "17:00"}}

	// From Jan 31 09:00, the next slot is the same day at 17:00.
	got, _ := spec.NextRun(time.Date(2026, 1, 31, 9, 0, 0, 0, tz), tz)
	want := time.Date(2026, 1, 31, 17, 0, 0, 0, tz)
	if !got.Equal(want) {
		t.Fatalf("got %v, want %v", got, want)
	}

	// Then February clamps 31 -> 28 (2026 is not a leap year).
	got, _ = spec.NextRun(want, tz)
	feb := time.Date(2026, 2, 28, 9, 0, 0, 0, tz)
	if !got.Equal(feb) {
		t.Fatalf("got %v, want clamped Feb 28 09:00 %v", got, feb)
	}
}

// A schedule with several times must still yield ONE next occurrence — the
// device does not fire everything it missed while offline. This is the case
// the catch-up window bounds, and "fire them all" is the obvious wrong
// implementation.
func TestNextRun_ReturnsSingleOccurrenceAfterLongGap(t *testing.T) {
	tz := mustTZ(t)
	spec := Spec{Repeat: RepeatDaily, Times: []string{"09:00", "13:00", "17:00"}}

	// Device offline all day, reconnecting at 17:05.
	got, ok := spec.NextRun(time.Date(2026, 8, 29, 17, 5, 0, 0, tz), tz)
	if !ok {
		t.Fatal("no next run")
	}
	want := time.Date(2026, 8, 30, 9, 0, 0, 0, tz)
	if !got.Equal(want) {
		t.Fatalf("got %v, want tomorrow's first slot %v", got, want)
	}
}

// Times is meaningless for interval; a stray list must not turn it into a
// wall-clock schedule.
func TestNextInterval_IgnoresTimes(t *testing.T) {
	tz := mustTZ(t)
	spec := Spec{Repeat: RepeatInterval, EveryMs: 3600000, Times: []string{"09:00"}}

	after := time.Date(2026, 8, 29, 12, 0, 0, 0, tz)
	got, ok := spec.NextRun(after, tz)
	if !ok {
		t.Fatal("no next run")
	}
	if want := after.Add(time.Hour); !got.Equal(want) {
		t.Fatalf("got %v, want %v (interval must ignore times)", got, want)
	}
}

// All occurrences of one schedule shift by the same offset, which is what
// keeps DejitterAnchor reversible. See JitterOffset's comment.
func TestJitter_SharedAcrossTimesAndReversible(t *testing.T) {
	tz := mustTZ(t)
	spec := Spec{Repeat: RepeatDaily, Times: []string{"09:00", "17:00"}}
	const dev, sch = "device-1", "schedule-1"

	first, ok := NextRunForDevice(spec, time.Date(2026, 8, 29, 8, 0, 0, 0, tz), tz, dev, sch)
	if !ok {
		t.Fatal("no first run")
	}
	// Feeding a jittered occurrence back in requires reversing it first.
	anchor := DejitterAnchor(spec.Repeat, first, dev, sch)
	if want := time.Date(2026, 8, 29, 9, 0, 0, 0, tz); !anchor.Equal(want) {
		t.Fatalf("dejittered anchor = %v, want %v", anchor, want)
	}

	second, _ := NextRunForDevice(spec, anchor, tz, dev, sch)
	secondAnchor := DejitterAnchor(spec.Repeat, second, dev, sch)
	if want := time.Date(2026, 8, 29, 17, 0, 0, 0, tz); !secondAnchor.Equal(want) {
		t.Fatalf("second dejittered = %v, want %v", secondAnchor, want)
	}

	// Same offset on both, so the 8h spacing the user asked for is preserved.
	if first.Sub(anchor) != second.Sub(secondAnchor) {
		t.Fatalf("offsets differ: %v vs %v", first.Sub(anchor), second.Sub(secondAnchor))
	}
}
