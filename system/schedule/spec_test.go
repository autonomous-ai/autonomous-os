package schedule

import (
	"fmt"
	"testing"
	"time"
)

func mustLoadLocation(t *testing.T, name string) *time.Location {
	t.Helper()
	loc, err := time.LoadLocation(name)
	if err != nil {
		t.Fatalf("LoadLocation(%q): %v", name, err)
	}
	return loc
}

func TestNextRun_Daily(t *testing.T) {
	tz := mustLoadLocation(t, "Asia/Ho_Chi_Minh")
	spec := Spec{Repeat: RepeatDaily, Time: "08:00"}

	// Before today's slot -> fires today.
	after := time.Date(2026, 8, 26, 7, 0, 0, 0, tz)
	got, ok := spec.NextRun(after, tz)
	if !ok {
		t.Fatal("expected ok=true")
	}
	want := time.Date(2026, 8, 26, 8, 0, 0, 0, tz)
	if !got.Equal(want) {
		t.Errorf("got %v, want %v", got, want)
	}

	// After today's slot (or exactly at it) -> rolls to tomorrow.
	after2 := time.Date(2026, 8, 26, 8, 0, 0, 0, tz)
	got2, ok2 := spec.NextRun(after2, tz)
	if !ok2 {
		t.Fatal("expected ok=true")
	}
	want2 := time.Date(2026, 8, 27, 8, 0, 0, 0, tz)
	if !got2.Equal(want2) {
		t.Errorf("got %v, want %v (must not re-fire the exact instant just passed)", got2, want2)
	}
}

// America/Los_Angeles, 2026-03-08: clocks spring forward at 02:00 -> 03:00, so
// 02:00-02:59:59 does not exist that day. A 02:30 daily schedule must still
// produce exactly one candidate for that calendar day (the stdlib normalizes
// the nonexistent wall-clock time to a single real instant instead of us
// scanning for one) and the following day's occurrence must still be found.
func TestNextRun_DailyAcrossDSTSpringForward(t *testing.T) {
	tz := mustLoadLocation(t, "America/Los_Angeles")
	spec := Spec{Repeat: RepeatDaily, Time: "02:30"}

	after := time.Date(2026, 3, 7, 12, 0, 0, 0, tz) // the day before
	got, ok := spec.NextRun(after, tz)
	if !ok {
		t.Fatal("expected ok=true")
	}
	if got.Month() != time.March || got.Day() != 8 {
		t.Fatalf("expected the gap day itself (Mar 8), got %v", got)
	}

	// Exactly one candidate that day: asking again strictly after it must land
	// on the NEXT day, not repeat Mar 8.
	got2, ok2 := spec.NextRun(got, tz)
	if !ok2 {
		t.Fatal("expected ok=true")
	}
	if got2.Day() != 9 {
		t.Fatalf("fired twice on the gap day: second occurrence = %v, want Mar 9", got2)
	}
}

// America/Los_Angeles, 2026-11-01: clocks fall back at 02:00 -> 01:00, so
// 01:00-01:59:59 occurs twice. A 01:30 daily schedule must fire ONCE for that
// calendar day, not twice — verified the same way as the spring-forward case:
// exactly one candidate is produced for the day, and the next call lands on
// the following day.
func TestNextRun_DailyAcrossDSTFallBack(t *testing.T) {
	tz := mustLoadLocation(t, "America/Los_Angeles")
	spec := Spec{Repeat: RepeatDaily, Time: "01:30"}

	after := time.Date(2026, 10, 31, 12, 0, 0, 0, tz)
	got, ok := spec.NextRun(after, tz)
	if !ok {
		t.Fatal("expected ok=true")
	}
	if got.Month() != time.November || got.Day() != 1 {
		t.Fatalf("expected the fall-back day itself (Nov 1), got %v", got)
	}

	got2, ok2 := spec.NextRun(got, tz)
	if !ok2 {
		t.Fatal("expected ok=true")
	}
	if got2.Day() != 2 {
		t.Fatalf("fired twice on the fall-back day: second occurrence = %v, want Nov 2", got2)
	}
}

func TestNextRun_WeeklyPicksNextListedDay(t *testing.T) {
	tz := mustLoadLocation(t, "Asia/Ho_Chi_Minh")
	// Mon-Fri at 08:00 (ISO weekdays 1-5).
	spec := Spec{Repeat: RepeatWeekly, Days: []int{1, 2, 3, 4, 5}, Time: "08:00"}

	// Friday 2026-08-28 at 09:00 -> next listed day is Monday 2026-08-31.
	after := time.Date(2026, 8, 28, 9, 0, 0, 0, tz)
	if after.Weekday() != time.Friday {
		t.Fatalf("test fixture bug: %v is not a Friday", after)
	}
	got, ok := spec.NextRun(after, tz)
	if !ok {
		t.Fatal("expected ok=true")
	}
	want := time.Date(2026, 8, 31, 8, 0, 0, 0, tz)
	if !got.Equal(want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

// The canonical `days` convention (per controller ruling after review) is
// 0=Sunday..6=Saturday, matching Go's own time.Weekday and the backend's
// tagged proto — NOT ISO-8601 (Monday=1..Sunday=7), which was this package's
// original (wrong) guess. The two conventions agree for every day except
// Sunday, so this is the one case that can actually catch a regression; 7 is
// also accepted as an alias for Sunday.
func TestNextRun_WeeklySundayConvention(t *testing.T) {
	tz := time.UTC
	// 2026-08-24 is a Monday; the next Sunday is 2026-08-30.
	after := time.Date(2026, 8, 24, 9, 0, 0, 0, tz)
	if after.Weekday() != time.Monday {
		t.Fatalf("test fixture bug: %v is not a Monday", after)
	}
	want := time.Date(2026, 8, 30, 8, 0, 0, 0, tz)

	for _, sundayValue := range []int{0, 7} {
		spec := Spec{Repeat: RepeatWeekly, Days: []int{sundayValue}, Time: "08:00"}
		got, ok := spec.NextRun(after, tz)
		if !ok {
			t.Fatalf("days=[%d]: expected ok=true", sundayValue)
		}
		if !got.Equal(want) {
			t.Errorf("days=[%d]: got %v, want %v (canonical Sunday=0, with 7 accepted as an alias)", sundayValue, got, want)
		}
	}
}

// day_of_month=31 in February -> the 28th (29th in a leap year, checked
// separately below).
func TestNextRun_MonthlyClampsToLastDayOfShortMonth(t *testing.T) {
	tz := time.UTC
	spec := Spec{Repeat: RepeatMonthly, DayOfMonth: 31, Time: "09:00"}

	after := time.Date(2026, 1, 31, 10, 0, 0, 0, tz) // already past Jan 31 09:00
	got, ok := spec.NextRun(after, tz)
	if !ok {
		t.Fatal("expected ok=true")
	}
	want := time.Date(2026, 2, 28, 9, 0, 0, 0, tz) // 2026 is not a leap year
	if !got.Equal(want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

func TestNextRun_MonthlyClampsToLastDayOfLeapFebruary(t *testing.T) {
	tz := time.UTC
	spec := Spec{Repeat: RepeatMonthly, DayOfMonth: 31, Time: "09:00"}

	after := time.Date(2028, 1, 31, 10, 0, 0, 0, tz) // 2028 IS a leap year
	got, ok := spec.NextRun(after, tz)
	if !ok {
		t.Fatal("expected ok=true")
	}
	want := time.Date(2028, 2, 29, 9, 0, 0, 0, tz)
	if !got.Equal(want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

func TestNextRun_IntervalAnchorsOnLastRun(t *testing.T) {
	spec := Spec{Repeat: RepeatInterval, EveryMs: 1_800_000} // 30 minutes, in milliseconds per the wire contract
	lastRun := time.Date(2026, 8, 26, 10, 0, 0, 0, time.UTC)

	got, ok := spec.NextRun(lastRun, time.UTC)
	if !ok {
		t.Fatal("expected ok=true")
	}
	want := lastRun.Add(30 * time.Minute)
	if !got.Equal(want) {
		t.Errorf("got %v, want %v (anchored on the passed-in last run, not wall clock)", got, want)
	}
}

// The device is the last line of defence against a near-zero interval — there
// is no rate limiting anywhere else in the send path (SendSystemChatMessage is
// fire-and-forget straight into the LLM). A misconfigured or malicious
// every_ms below 5 minutes must clamp to the floor, not spam turns.
func TestNextRun_IntervalClampsToFiveMinuteFloor(t *testing.T) {
	spec := Spec{Repeat: RepeatInterval, EveryMs: 1000} // 1 second — far under the floor
	lastRun := time.Date(2026, 8, 26, 10, 0, 0, 0, time.UTC)

	got, ok := spec.NextRun(lastRun, time.UTC)
	if !ok {
		t.Fatal("expected ok=true")
	}
	want := lastRun.Add(5 * time.Minute)
	if !got.Equal(want) {
		t.Errorf("got %v, want %v (clamped to the 5-minute floor)", got, want)
	}
}

func TestNextRun_IntervalZeroNeverFires(t *testing.T) {
	spec := Spec{Repeat: RepeatInterval, EveryMs: 0}
	if _, ok := spec.NextRun(time.Now(), time.UTC); ok {
		t.Error("every_ms=0 must never fire")
	}
}

func TestNextRun_OnceReturnsFalseAfterItHasPassed(t *testing.T) {
	at := time.Date(2026, 8, 26, 8, 0, 0, 0, time.UTC)
	spec := Spec{Repeat: RepeatOnce, At: &at}

	// Before At -> fires.
	before := at.Add(-time.Hour)
	got, ok := spec.NextRun(before, time.UTC)
	if !ok || !got.Equal(at) {
		t.Fatalf("NextRun(before) = %v, %v; want %v, true", got, ok, at)
	}

	// At or after At -> spent, never again.
	if _, ok := spec.NextRun(at, time.UTC); ok {
		t.Error("a once schedule must not fire again once At has been reached")
	}
	after := at.Add(time.Hour)
	if _, ok := spec.NextRun(after, time.UTC); ok {
		t.Error("a once schedule must not fire again after At has passed")
	}
}

func TestNextRun_ManualNeverFires(t *testing.T) {
	now := time.Now()
	_, ok := Spec{Repeat: RepeatManual}.NextRun(now, time.UTC)
	if ok {
		t.Error("a manual schedule must never report a next run")
	}
}

func TestNextRun_PastEndAtReturnsFalse(t *testing.T) {
	tz := time.UTC
	endAt := time.Date(2026, 8, 1, 0, 0, 0, 0, tz)

	// `after` already past end_at -> never again, regardless of cadence.
	spec := Spec{Repeat: RepeatDaily, Time: "08:00", EndAt: &endAt}
	after := time.Date(2026, 8, 26, 7, 0, 0, 0, tz)
	if _, ok := spec.NextRun(after, tz); ok {
		t.Error("a schedule whose end_at has already passed must not fire")
	}

	// end_at still in the future, but the computed occurrence would fall
	// beyond it -> also never again (the validity window closes mid-cadence).
	endAtSoon := time.Date(2026, 8, 26, 7, 30, 0, 0, tz)
	spec2 := Spec{Repeat: RepeatDaily, Time: "08:00", EndAt: &endAtSoon}
	if _, ok := spec2.NextRun(time.Date(2026, 8, 26, 7, 0, 0, 0, tz), tz); ok {
		t.Error("a computed occurrence past end_at must not be returned")
	}
}

// Same device id + schedule id -> same offset every call. Two different
// device ids -> different offsets (thundering-herd guard: many devices on the
// exact same cadence must not all fire in the same instant).
func TestNextRun_JitterIsDeterministicPerDevice(t *testing.T) {
	a1 := JitterOffset("device-A", "sched-1")
	a2 := JitterOffset("device-A", "sched-1")
	if a1 != a2 {
		t.Fatalf("same (device, schedule) produced different offsets: %v vs %v", a1, a2)
	}
	if a1 < -maxJitter || a1 > maxJitter {
		t.Fatalf("offset %v out of range [-%v, %v]", a1, maxJitter, maxJitter)
	}

	b1 := JitterOffset("device-B", "sched-1")
	if a1 == b1 {
		t.Errorf("different device ids produced the same offset (%v) for the same schedule — thundering-herd guard defeated", a1)
	}
}

// Regression for CRITICAL-1: JitterOffset used to hash into NANOSECONDS with a
// uint32 sum, so the modulo was a silent no-op and every input collapsed to a
// ~4.29-second window pinned at -maxJitter (a constant -5m for all practical
// purposes). A real spread must cover at least a few minutes across many
// distinct device ids, not a handful of seconds.
func TestJitterOffset_SpansSeveralMinutesAcrossManyDevices(t *testing.T) {
	minOffset, maxOffset := maxJitter, -maxJitter
	for i := 0; i < 200; i++ {
		off := JitterOffset(fmt.Sprintf("device-%d", i), "sched-1")
		if off < -maxJitter || off >= maxJitter {
			t.Fatalf("device-%d: offset %v out of range [-%v, %v)", i, off, maxJitter, maxJitter)
		}
		if off < minOffset {
			minOffset = off
		}
		if off > maxOffset {
			maxOffset = off
		}
	}
	if spread := maxOffset - minOffset; spread < 2*time.Minute {
		t.Fatalf("jitter spread across 200 device ids = %v (min=%v max=%v), want at least 2 minutes", spread, minOffset, maxOffset)
	}
}

func TestNextRunForDevice_AppliesJitterOnlyToWallClockCadences(t *testing.T) {
	tz := time.UTC
	after := time.Date(2026, 8, 26, 7, 0, 0, 0, tz)

	daily := Spec{Repeat: RepeatDaily, Time: "08:00"}
	base, _ := daily.NextRun(after, tz)
	jittered, ok := NextRunForDevice(daily, after, tz, "device-1", "sched-1")
	if !ok {
		t.Fatal("expected ok=true")
	}
	wantOffset := JitterOffset("device-1", "sched-1")
	if !jittered.Equal(base.Add(wantOffset)) {
		t.Errorf("jittered = %v, want base %v + offset %v", jittered, base, wantOffset)
	}

	// interval and once are exempt from jitter.
	lastRun := time.Date(2026, 8, 26, 10, 0, 0, 0, tz)
	interval := Spec{Repeat: RepeatInterval, EveryMs: 600_000} // 10 minutes — above the 5-minute floor
	gotInterval, ok := NextRunForDevice(interval, lastRun, tz, "device-1", "sched-2")
	if !ok {
		t.Fatal("expected ok=true")
	}
	if !gotInterval.Equal(lastRun.Add(10 * time.Minute)) {
		t.Errorf("interval schedule was jittered: got %v", gotInterval)
	}

	at := time.Date(2026, 8, 27, 0, 0, 0, 0, tz)
	once := Spec{Repeat: RepeatOnce, At: &at}
	gotOnce, ok := NextRunForDevice(once, after, tz, "device-1", "sched-3")
	if !ok {
		t.Fatal("expected ok=true")
	}
	if !gotOnce.Equal(at) {
		t.Errorf("once schedule was jittered: got %v, want exactly %v", gotOnce, at)
	}
}

// Regression for CRITICAL-2: DejitterAnchor must exactly reverse what
// NextRunForDevice applied, for every jittered cadence, so feeding a stored
// (jittered) NextRunAt back through it recovers the TRUE boundary NextRun
// itself understands.
func TestDejitterAnchor_ReversesNextRunForDeviceJitter(t *testing.T) {
	tz := time.UTC
	after := time.Date(2026, 8, 26, 7, 0, 0, 0, tz)

	for _, repeat := range []string{RepeatDaily, RepeatWeekly, RepeatMonthly} {
		spec := Spec{Repeat: repeat, Time: "08:00", Days: []int{3}, DayOfMonth: 15}
		base, ok := spec.NextRun(after, tz)
		if !ok {
			t.Fatalf("%s: expected ok=true", repeat)
		}
		jittered, ok := NextRunForDevice(spec, after, tz, "device-1", "sched-1")
		if !ok {
			t.Fatalf("%s: expected ok=true", repeat)
		}
		dejittered := DejitterAnchor(repeat, jittered, "device-1", "sched-1")
		if !dejittered.Equal(base) {
			t.Errorf("%s: DejitterAnchor(%v) = %v, want the unjittered base %v", repeat, jittered, dejittered, base)
		}
	}

	// interval/once/manual were never jittered — DejitterAnchor must be a
	// pure no-op for them, not subtract an offset that was never applied.
	for _, repeat := range []string{RepeatInterval, RepeatOnce, RepeatManual} {
		stamp := time.Date(2026, 8, 26, 12, 0, 0, 0, tz)
		if got := DejitterAnchor(repeat, stamp, "device-1", "sched-1"); !got.Equal(stamp) {
			t.Errorf("%s: DejitterAnchor must be a no-op, got %v, want %v", repeat, got, stamp)
		}
	}
}
