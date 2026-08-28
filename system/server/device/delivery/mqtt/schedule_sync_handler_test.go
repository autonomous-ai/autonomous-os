package mqtthandler

import (
	"encoding/json"
	"path/filepath"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/schedule"
)

func TestHandleScheduleSync_ReplacesStoreAndAcksNextRuns(t *testing.T) {
	store := schedule.NewStore(filepath.Join(t.TempDir(), "schedules.json"))
	now := time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC)

	payload := scheduleSyncPayload{
		Timezone: "UTC",
		Schedules: []schedule.Schedule{
			{ID: "s1", Name: "Daily briefing", Instructions: "Summarize my day", Enabled: true,
				Cadence: schedule.Spec{Repeat: schedule.RepeatDaily, Time: "08:00"}},
			{ID: "s2", Name: "Disabled one", Instructions: "should not ack", Enabled: false,
				Cadence: schedule.Spec{Repeat: schedule.RepeatDaily, Time: "09:00"}},
		},
	}

	applied, nextRunAt, err := applyScheduleSync(store, payload, "device-1", now)
	if err != nil {
		t.Fatalf("applyScheduleSync: %v", err)
	}
	if applied != 2 {
		t.Errorf("applied = %d, want 2", applied)
	}
	if _, ok := nextRunAt["s2"]; ok {
		t.Error("a disabled schedule must not appear in next_run_at")
	}
	got1, ok := nextRunAt["s1"]
	if !ok {
		t.Fatal("enabled schedule missing from next_run_at")
	}
	if _, err := time.Parse(time.RFC3339, got1); err != nil {
		t.Errorf("next_run_at[s1] = %q is not RFC3339: %v", got1, err)
	}

	stored, err := store.Load()
	if err != nil || len(stored) != 2 {
		t.Fatalf("store.Load() = %+v, %v", stored, err)
	}
	var s1 schedule.Schedule
	for _, s := range stored {
		if s.ID == "s1" {
			s1 = s
		}
	}
	if s1.NextRunAt.IsZero() {
		t.Error("next_run_at was computed for the ack but not persisted to the store")
	}
	if loc := store.Timezone(); loc.String() != "UTC" {
		t.Errorf("Timezone() = %v, want UTC", loc)
	}
}

// Deleting the last schedule must actually clear the device.
func TestHandleScheduleSync_EmptyListClearsAllSchedules(t *testing.T) {
	store := schedule.NewStore(filepath.Join(t.TempDir(), "schedules.json"))
	now := time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC)

	seed := scheduleSyncPayload{Timezone: "UTC", Schedules: []schedule.Schedule{
		{ID: "s1", Enabled: true, Cadence: schedule.Spec{Repeat: schedule.RepeatDaily, Time: "08:00"}},
	}}
	if _, _, err := applyScheduleSync(store, seed, "device-1", now); err != nil {
		t.Fatalf("seed: %v", err)
	}
	if got, _ := store.Load(); len(got) != 1 {
		t.Fatalf("seed did not land: %+v", got)
	}

	applied, nextRunAt, err := applyScheduleSync(store, scheduleSyncPayload{Timezone: "UTC"}, "device-1", now)
	if err != nil {
		t.Fatalf("clear: %v", err)
	}
	if applied != 0 || len(nextRunAt) != 0 {
		t.Errorf("applied=%d nextRunAt=%v, want both empty", applied, nextRunAt)
	}

	got, err := store.Load()
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if len(got) != 0 {
		t.Fatalf("schedules were not cleared: %+v", got)
	}
}

func TestApplyScheduleSync_InvalidTimezoneFallsBackButStillApplies(t *testing.T) {
	store := schedule.NewStore(filepath.Join(t.TempDir(), "schedules.json"))
	now := time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC)

	payload := scheduleSyncPayload{
		Timezone: "Not/A/Real/Zone",
		Schedules: []schedule.Schedule{
			{ID: "s1", Enabled: true, Cadence: schedule.Spec{Repeat: schedule.RepeatDaily, Time: "08:00"}},
		},
	}
	applied, nextRunAt, err := applyScheduleSync(store, payload, "device-1", now)
	if err != nil {
		t.Fatalf("applyScheduleSync must not fail over a bad timezone: %v", err)
	}
	if applied != 1 || len(nextRunAt) != 1 {
		t.Fatalf("applied=%d nextRunAt=%v, want the schedule to still be applied", applied, nextRunAt)
	}
}

// I2 (phase-5 review): a real, enabled schedule that turns out to be
// un-computable (here: a malformed "time") must not silently vanish from the
// device with no trace — schedule.SyncSchedules logs a slog.Warn (id +
// repeat) on this path. Behaviorally, the sync must still apply (replace the
// store) and ack success for the OTHER schedules; this one is simply omitted
// from next_run_at rather than failing the whole sync.
func TestApplyScheduleSync_UncomputableScheduleStillAppliesButOmitsNextRun(t *testing.T) {
	store := schedule.NewStore(filepath.Join(t.TempDir(), "schedules.json"))
	now := time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC)

	payload := scheduleSyncPayload{
		Timezone: "UTC",
		Schedules: []schedule.Schedule{
			{ID: "s1", Enabled: true, Cadence: schedule.Spec{Repeat: schedule.RepeatDaily, Time: "not-a-time"}},
		},
	}
	applied, nextRunAt, err := applyScheduleSync(store, payload, "device-1", now)
	if err != nil {
		t.Fatalf("applyScheduleSync must not fail over one uncomputable schedule: %v", err)
	}
	if applied != 1 {
		t.Errorf("applied = %d, want 1 (the schedule is still replaced into the store)", applied)
	}
	if _, ok := nextRunAt["s1"]; ok {
		t.Error("an uncomputable schedule must not appear in next_run_at")
	}
	stored, err := store.Load()
	if err != nil || len(stored) != 1 {
		t.Fatalf("store.Load() = %+v, %v", stored, err)
	}
}

// scheduleTimezone was inlined into schedule.SyncSchedules — this now checks
// the shared helper (schedule.ResolveTimezone) that applyScheduleSync
// delegates to under the hood, via SyncSchedules.
func TestScheduleTimezoneResolution(t *testing.T) {
	if loc, err := schedule.ResolveTimezone(""); err != nil || loc != time.UTC {
		t.Errorf("ResolveTimezone(\"\") = %v, %v; want UTC, nil", loc, err)
	}
	if loc, err := schedule.ResolveTimezone("Asia/Ho_Chi_Minh"); err != nil || loc.String() != "Asia/Ho_Chi_Minh" {
		t.Errorf("ResolveTimezone(valid) = %v, %v", loc, err)
	}
	if loc, err := schedule.ResolveTimezone("garbage"); err == nil || loc != time.UTC {
		t.Errorf("ResolveTimezone(garbage) = %v, %v; want UTC, err", loc, err)
	}
}

// I3 (phase-5 review): no test previously parsed actual wire JSON. This
// unmarshals a literal envelope using the CANONICAL field names the
// controller ruling settled on — "every_ms" (not "interval_seconds") and the
// 0=Sunday..6=Saturday weekday convention (matching the tagged backend proto
// and Go's own time.Weekday) — and asserts every field lands correctly.
func TestScheduleSyncPayload_ParsesCanonicalWireJSON(t *testing.T) {
	raw := `{
		"timezone": "Asia/Ho_Chi_Minh",
		"schedules": [
			{
				"id": "sched-1",
				"name": "Daily briefing",
				"instructions": "Summarize my day",
				"enabled": true,
				"schedule": {
					"repeat": "weekly",
					"time": "08:00",
					"days": [1, 2, 3, 4, 5]
				},
				"end_at": null
			},
			{
				"id": "sched-2",
				"name": "Every 10 minutes",
				"instructions": "Check the mailbox",
				"enabled": true,
				"schedule": {
					"repeat": "interval",
					"every_ms": 600000
				},
				"end_at": null
			},
			{
				"id": "sched-3",
				"name": "First of the month",
				"instructions": "Send the invoice",
				"enabled": true,
				"schedule": {
					"repeat": "monthly",
					"day_of_month": 1,
					"time": "09:00"
				},
				"end_at": null
			},
			{
				"id": "sched-4",
				"name": "Sunday check-in",
				"instructions": "Weekly review",
				"enabled": true,
				"schedule": {
					"repeat": "weekly",
					"time": "10:00",
					"days": [0]
				},
				"end_at": "2027-01-01T00:00:00Z"
			}
		]
	}`

	var payload scheduleSyncPayload
	if err := json.Unmarshal([]byte(raw), &payload); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}

	if payload.Timezone != "Asia/Ho_Chi_Minh" {
		t.Errorf("Timezone = %q", payload.Timezone)
	}
	if len(payload.Schedules) != 4 {
		t.Fatalf("Schedules = %+v, want 4", payload.Schedules)
	}

	s1 := payload.Schedules[0]
	if s1.ID != "sched-1" || s1.Name != "Daily briefing" || s1.Instructions != "Summarize my day" || !s1.Enabled {
		t.Errorf("s1 top-level fields = %+v", s1)
	}
	if s1.Cadence.Repeat != schedule.RepeatWeekly || s1.Cadence.Time != "08:00" {
		t.Errorf("s1.Cadence = %+v", s1.Cadence)
	}
	if len(s1.Cadence.Days) != 5 || s1.Cadence.Days[0] != 1 || s1.Cadence.Days[4] != 5 {
		t.Errorf("s1.Cadence.Days = %v", s1.Cadence.Days)
	}
	if s1.EndAt != nil {
		t.Errorf("s1.EndAt = %v, want nil", s1.EndAt)
	}

	s2 := payload.Schedules[1]
	if s2.Cadence.Repeat != schedule.RepeatInterval || s2.Cadence.EveryMs != 600000 {
		t.Errorf("s2.Cadence = %+v, want every_ms=600000 parsed as MILLISECONDS (not interval_seconds)", s2.Cadence)
	}

	s3 := payload.Schedules[2]
	if s3.Cadence.Repeat != schedule.RepeatMonthly || s3.Cadence.DayOfMonth != 1 || s3.Cadence.Time != "09:00" {
		t.Errorf("s3.Cadence = %+v", s3.Cadence)
	}

	s4 := payload.Schedules[3]
	if len(s4.Cadence.Days) != 1 || s4.Cadence.Days[0] != 0 {
		t.Errorf("s4.Cadence.Days = %v, want [0] (canonical Sunday)", s4.Cadence.Days)
	}
	if s4.EndAt == nil || !s4.EndAt.Equal(time.Date(2027, 1, 1, 0, 0, 0, 0, time.UTC)) {
		t.Errorf("s4.EndAt = %v, want 2027-01-01T00:00:00Z", s4.EndAt)
	}
}

// TestBuildScheduleRunReportData_IncludesNextRunAtWhenSet is the CRITICAL-2
// regression from the final review: fire() computes and persists the next
// occurrence but, before this fix, never forwarded it onto the schedule.run
// ack — the web UI's cadence column showed "in 16 hours" until a schedule's
// first fire and then permanently "now" for the rest of its life
// (formatCadence.ts treats any non-positive diff as "now"). next_run_at must
// be present on the wire and parse as RFC3339.
func TestBuildScheduleRunReportData_IncludesNextRunAtWhenSet(t *testing.T) {
	next := time.Date(2026, 8, 27, 8, 0, 0, 0, time.UTC)
	rr := schedule.RunReport{
		ScheduleID:  "s1",
		RunID:       "run-1",
		StartedAt:   time.Date(2026, 8, 26, 8, 0, 0, 0, time.UTC),
		SendLatency: 12 * time.Millisecond,
		Status:      "success",
		Summary:     "Daily briefing",
		NextRunAt:   next,
	}

	data := buildScheduleRunReportData(rr)

	got, ok := data["next_run_at"]
	if !ok {
		t.Fatal("next_run_at key missing from schedule.run report payload")
	}
	s, ok := got.(string)
	if !ok {
		t.Fatalf("next_run_at = %T, want string", got)
	}
	parsed, err := time.Parse(time.RFC3339, s)
	if err != nil {
		t.Fatalf("next_run_at = %q is not RFC3339: %v", s, err)
	}
	if !parsed.Equal(next) {
		t.Errorf("next_run_at = %v, want %v", parsed, next)
	}
}

// TestBuildScheduleRunReportData_OmitsNextRunAtWhenZero covers the two paths
// that never advance NextRunAt: a failed fire (I5 — must not burn the
// occurrence) and a manual "Run now" (must not perturb the regular cadence).
// Neither should fabricate a next_run_at key.
func TestBuildScheduleRunReportData_OmitsNextRunAtWhenZero(t *testing.T) {
	rr := schedule.RunReport{ScheduleID: "s1", Status: "failure", Summary: "boom"}

	data := buildScheduleRunReportData(rr)

	if v, ok := data["next_run_at"]; ok {
		t.Errorf("next_run_at should be omitted when RunReport.NextRunAt is zero, got %v", v)
	}
}

func TestScheduleKindsAreDistinct(t *testing.T) {
	if domain.KindScheduleSync == domain.KindScheduleRun {
		t.Fatal("schedule.sync and schedule.run must be distinct kinds")
	}
	if domain.KindScheduleSync != "schedule.sync" {
		t.Errorf("KindScheduleSync = %q, want schedule.sync", domain.KindScheduleSync)
	}
	if domain.KindScheduleRun != "schedule.run" {
		t.Errorf("KindScheduleRun = %q, want schedule.run", domain.KindScheduleRun)
	}
}
