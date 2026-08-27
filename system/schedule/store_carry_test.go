package schedule

import (
	"path/filepath"
	"testing"
	"time"
)

func carryStore(t *testing.T) *Store {
	t.Helper()
	return NewStore(filepath.Join(t.TempDir(), "schedules.json"))
}

// wireShaped is what a schedule.sync actually produces: the backend-owned
// fields populated, and every device-local bookkeeping field zero because the
// wire has no representation for them.
func wireShaped(id, name string) Schedule {
	return Schedule{
		ID:      id,
		Name:    name,
		Enabled: true,
		Cadence: Spec{Repeat: "daily", Time: "08:00"},
	}
}

// The bug this guards: a sync used to overwrite run history with the wire's
// zeros, so the device forgot it had ever run a task.
func TestReplace_PreservesRunHistoryAcrossSync(t *testing.T) {
	s := carryStore(t)
	ran := time.Date(2026, 8, 26, 21, 19, 20, 0, time.UTC)

	seeded := wireShaped("sched-1", "App-made task")
	seeded.LastRunAt = ran
	seeded.LastRunStatus = "success"
	if err := s.Replace([]Schedule{seeded}); err != nil {
		t.Fatalf("seed: %v", err)
	}

	// A later sync carries the same schedule, renamed, with zeroed bookkeeping.
	if err := s.Replace([]Schedule{wireShaped("sched-1", "App-made task (renamed)")}); err != nil {
		t.Fatalf("resync: %v", err)
	}

	got, ok := s.Get("sched-1")
	if !ok {
		t.Fatal("schedule vanished across sync")
	}
	if !got.LastRunAt.Equal(ran) {
		t.Errorf("last_run_at = %v, want it preserved as %v", got.LastRunAt, ran)
	}
	if got.LastRunStatus != "success" {
		t.Errorf("last_run_status = %q, want it preserved as success", got.LastRunStatus)
	}
	// The backend-owned half must still be updated by the sync.
	if got.Name != "App-made task (renamed)" {
		t.Errorf("name = %q, want the synced value", got.Name)
	}
}

// The consequential half. LastFailedOccurrence is what Runner.fire uses to
// suppress repeated failure acks for one occurrence; zeroing it re-opens the
// duplicate schedule_run rows that suppression exists to prevent.
func TestReplace_PreservesLastFailedOccurrence(t *testing.T) {
	s := carryStore(t)
	occurrence := time.Date(2026, 8, 27, 8, 0, 0, 0, time.UTC)

	seeded := wireShaped("sched-1", "Flaky task")
	seeded.LastRunStatus = "failure"
	seeded.LastFailedOccurrence = occurrence
	if err := s.Replace([]Schedule{seeded}); err != nil {
		t.Fatalf("seed: %v", err)
	}

	if err := s.Replace([]Schedule{wireShaped("sched-1", "Flaky task")}); err != nil {
		t.Fatalf("resync: %v", err)
	}

	got, _ := s.Get("sched-1")
	if !got.LastFailedOccurrence.Equal(occurrence) {
		t.Errorf("last_failed_occurrence = %v, want it preserved as %v", got.LastFailedOccurrence, occurrence)
	}
	if got.LastRunStatus != "failure" {
		t.Errorf("last_run_status = %q, want failure preserved", got.LastRunStatus)
	}
}

// ReplaceWithTimezone is the path schedule.sync actually takes, so it needs the
// same guarantee — and must still apply the timezone.
func TestReplaceWithTimezone_PreservesRunHistory(t *testing.T) {
	s := carryStore(t)
	ran := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)

	seeded := wireShaped("sched-1", "Task")
	seeded.LastRunAt = ran
	seeded.LastRunStatus = "success"
	if err := s.ReplaceWithTimezone([]Schedule{seeded}, "UTC"); err != nil {
		t.Fatalf("seed: %v", err)
	}

	if err := s.ReplaceWithTimezone([]Schedule{wireShaped("sched-1", "Task")}, "Asia/Ho_Chi_Minh"); err != nil {
		t.Fatalf("resync: %v", err)
	}

	got, _ := s.Get("sched-1")
	if !got.LastRunAt.Equal(ran) {
		t.Errorf("last_run_at = %v, want preserved", got.LastRunAt)
	}
	if tz := s.Timezone().String(); tz != "Asia/Ho_Chi_Minh" {
		t.Errorf("timezone = %q, want the newly synced one", tz)
	}
}

// A schedule the backend no longer sends is genuinely gone — carrying
// bookkeeping must not resurrect it.
func TestReplace_DroppedScheduleStaysDropped(t *testing.T) {
	s := carryStore(t)
	seeded := wireShaped("sched-1", "Doomed")
	seeded.LastRunStatus = "success"
	if err := s.Replace([]Schedule{seeded}); err != nil {
		t.Fatalf("seed: %v", err)
	}

	if err := s.Replace([]Schedule{wireShaped("sched-2", "Survivor")}); err != nil {
		t.Fatalf("resync: %v", err)
	}

	if _, ok := s.Get("sched-1"); ok {
		t.Error("deleted schedule was resurrected by the carry-forward")
	}
	all, _ := s.Load()
	if len(all) != 1 {
		t.Fatalf("got %d schedules, want 1", len(all))
	}
}

// A brand-new schedule has no prior state; it must not inherit a neighbour's.
func TestReplace_NewScheduleGetsNoInheritedHistory(t *testing.T) {
	s := carryStore(t)
	seeded := wireShaped("sched-1", "Existing")
	seeded.LastRunAt = time.Date(2026, 8, 26, 9, 0, 0, 0, time.UTC)
	seeded.LastRunStatus = "success"
	if err := s.Replace([]Schedule{seeded}); err != nil {
		t.Fatalf("seed: %v", err)
	}

	if err := s.Replace([]Schedule{seeded, wireShaped("sched-2", "Brand new")}); err != nil {
		t.Fatalf("resync: %v", err)
	}

	fresh, _ := s.Get("sched-2")
	if !fresh.LastRunAt.IsZero() || fresh.LastRunStatus != "" {
		t.Errorf("new schedule inherited history: %+v", fresh)
	}
}

// An explicit non-zero value from the caller must win over what is on disk —
// otherwise the runner could never record a NEW run over an older one.
func TestReplace_IncomingValueWinsOverStored(t *testing.T) {
	s := carryStore(t)
	older := time.Date(2026, 8, 26, 9, 0, 0, 0, time.UTC)
	newer := time.Date(2026, 8, 27, 9, 0, 0, 0, time.UTC)

	seeded := wireShaped("sched-1", "Task")
	seeded.LastRunAt = older
	seeded.LastRunStatus = "failure"
	if err := s.Replace([]Schedule{seeded}); err != nil {
		t.Fatalf("seed: %v", err)
	}

	updated := wireShaped("sched-1", "Task")
	updated.LastRunAt = newer
	updated.LastRunStatus = "success"
	if err := s.Replace([]Schedule{updated}); err != nil {
		t.Fatalf("update: %v", err)
	}

	got, _ := s.Get("sched-1")
	if !got.LastRunAt.Equal(newer) {
		t.Errorf("last_run_at = %v, want the incoming %v", got.LastRunAt, newer)
	}
	if got.LastRunStatus != "success" {
		t.Errorf("last_run_status = %q, want the incoming success", got.LastRunStatus)
	}
}
