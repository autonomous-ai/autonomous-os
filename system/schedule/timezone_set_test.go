package schedule

import (
	"path/filepath"
	"testing"
	"time"
)

// A timezone change arrives on its own downlink, not as part of a
// schedule.sync — and the runner resolves every wall-clock cadence against
// Store.Timezone(). These pin the store half of that fix: without SetTimezone
// the store keeps whatever zone the last sync carried, and a device moved from
// UTC to Asia/Saigon goes on firing on the old zone indefinitely.

func TestSetTimezone_UpdatesAndReportsChange(t *testing.T) {
	s := NewStore(filepath.Join(t.TempDir(), "schedules.json"))
	if err := s.ReplaceWithTimezone([]Schedule{{ID: "a", Name: "x"}}, "Etc/UTC"); err != nil {
		t.Fatalf("seed: %v", err)
	}

	changed, err := s.SetTimezone("Asia/Saigon")
	if err != nil {
		t.Fatalf("SetTimezone: %v", err)
	}
	if !changed {
		t.Fatal("changed = false, want true for a genuinely new zone")
	}
	if got := s.Timezone().String(); got != "Asia/Saigon" {
		t.Fatalf("Timezone() = %q, want Asia/Saigon", got)
	}
}

// A repeat downlink must not report a change — callers skip the recompute and
// the upward publish on that signal, so a chatty duplicate would otherwise
// spam a sync result on every message.
func TestSetTimezone_NoOpOnRepeat(t *testing.T) {
	s := NewStore(filepath.Join(t.TempDir(), "schedules.json"))
	if err := s.ReplaceWithTimezone(nil, "Asia/Saigon"); err != nil {
		t.Fatalf("seed: %v", err)
	}
	changed, err := s.SetTimezone("Asia/Saigon")
	if err != nil {
		t.Fatalf("SetTimezone: %v", err)
	}
	if changed {
		t.Fatal("changed = true for an identical zone, want false")
	}
}

// The schedule list must survive a timezone-only write — this is the whole
// point of not routing it through ReplaceWithTimezone, which would need the
// caller to hand back the schedules and could drop run bookkeeping.
func TestSetTimezone_PreservesSchedules(t *testing.T) {
	s := NewStore(filepath.Join(t.TempDir(), "schedules.json"))
	seed := []Schedule{
		{ID: "a", Name: "Drink some water", Enabled: true,
			Cadence: Spec{Repeat: RepeatDaily, Time: "11:00", Times: []string{"11:00", "11:25"}}},
	}
	if err := s.ReplaceWithTimezone(seed, "Etc/UTC"); err != nil {
		t.Fatalf("seed: %v", err)
	}

	if _, err := s.SetTimezone("Asia/Saigon"); err != nil {
		t.Fatalf("SetTimezone: %v", err)
	}

	got, err := s.Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if len(got) != 1 || got[0].ID != "a" {
		t.Fatalf("schedules = %+v, want the seeded one preserved", got)
	}
	if len(got[0].Cadence.Times) != 2 {
		t.Fatalf("times = %v, want both preserved", got[0].Cadence.Times)
	}
}

// The bug this fixes, end to end at the store level: the same 11:00 cadence
// must resolve to a DIFFERENT instant once the zone changes. Before the fix
// the store stayed on Etc/UTC and 11:00 kept meaning 11:00Z (18:00 Saigon).
func TestSetTimezone_ChangesResolvedFireTime(t *testing.T) {
	s := NewStore(filepath.Join(t.TempDir(), "schedules.json"))
	spec := Spec{Repeat: RepeatDaily, Time: "11:00"}
	if err := s.ReplaceWithTimezone([]Schedule{{ID: "a", Enabled: true, Cadence: spec}}, "Etc/UTC"); err != nil {
		t.Fatalf("seed: %v", err)
	}

	after := time.Date(2026, 9, 9, 4, 0, 0, 0, time.UTC)
	utcNext, ok := spec.NextRun(after, s.Timezone())
	if !ok {
		t.Fatal("no next run under UTC")
	}

	if _, err := s.SetTimezone("Asia/Saigon"); err != nil {
		t.Fatalf("SetTimezone: %v", err)
	}
	sgnNext, ok := spec.NextRun(after, s.Timezone())
	if !ok {
		t.Fatal("no next run under Asia/Saigon")
	}

	if utcNext.Equal(sgnNext) {
		t.Fatalf("fire time unchanged (%v) after a timezone change — the store did not re-anchor", utcNext)
	}
	// 11:00 Saigon is 04:00Z. Anchored at exactly 04:00Z the same-day slot is
	// already spent, so the correct answer rolls to tomorrow 04:00Z — the
	// point being that the WALL CLOCK now lands on 11:00 local, not 11:00Z.
	if h, m, _ := sgnNext.In(mustLoad(t, "Asia/Saigon")).Clock(); h != 11 || m != 0 {
		t.Fatalf("Saigon next resolves to %02d:%02d local, want 11:00", h, m)
	}
	if h, m, _ := utcNext.UTC().Clock(); h != 11 || m != 0 {
		t.Fatalf("UTC next resolves to %02d:%02d UTC, want 11:00", h, m)
	}
}

func mustLoad(t *testing.T, name string) *time.Location {
	t.Helper()
	loc, err := time.LoadLocation(name)
	if err != nil {
		t.Fatalf("load %s: %v", name, err)
	}
	return loc
}
