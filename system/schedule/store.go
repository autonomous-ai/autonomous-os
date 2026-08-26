package schedule

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// Schedule is one recurring (or one-shot) agent task, exactly as the backend's
// schedule.sync payload describes it, plus a handful of store-only bookkeeping
// fields the device needs to run the scheduler across restarts.
type Schedule struct {
	ID           string `json:"id"`
	Name         string `json:"name"`
	Instructions string `json:"instructions"`
	Enabled      bool   `json:"enabled"`

	// Cadence is the wire's nested "schedule" object. Named Cadence (not
	// Schedule) on the Go side only to avoid a Schedule.Schedule stutter — the
	// JSON tag still matches the wire contract exactly.
	Cadence Spec `json:"schedule"`

	// Rev is the backend-assigned revision of this row, carried straight
	// through from schedule.sync. The device never invents or increments it —
	// it only quotes it back as base_rev when proposing an edit, so the backend
	// can compare-and-swap. A row the device has never synced has rev 0, which
	// no backend row ever has (they start at 1), so a stale proposal is
	// naturally rejected rather than silently applied.
	Rev uint64 `json:"rev,omitempty"`

	// EndAt is a sibling of "schedule" on the wire, not nested inside it (see
	// Spec's doc comment). nil means "no expiry".
	EndAt *time.Time `json:"end_at,omitempty"`

	// NextRunAt, LastRunAt and LastRunStatus are NOT part of the backend wire
	// contract — they are local bookkeeping the runner needs to survive a
	// device restart without re-deriving everything from scratch. Persisted in
	// the same schedules.json (never config.json — see package doc in
	// runner.go) because they change together with the schedule they belong to.
	NextRunAt     time.Time `json:"next_run_at,omitempty"`
	LastRunAt     time.Time `json:"last_run_at,omitempty"`
	LastRunStatus string    `json:"last_run_status,omitempty"`

	// LastFailedOccurrence pins a recorded failure to the SPECIFIC due slot
	// (the schedule's NextRunAt at the time of that failed attempt) it
	// belongs to. Purely a runner concern (see fire()'s ack-suppression
	// logic): while an occurrence is being retried, NextRunAt stays fixed
	// (I5 — a failure must not burn it), so comparing this field against the
	// CURRENT NextRunAt is how the runner tells "this failure is a retry of
	// the occurrence already ack'd" from "this is the first failure of a
	// fresh occurrence" (the previous occurrence's NextRunAt is never equal
	// to the new one, so the comparison naturally resets once the schedule
	// advances or is re-anchored — nothing needs to explicitly clear this on
	// those paths). Never part of the wire contract.
	LastFailedOccurrence time.Time `json:"last_failed_occurrence,omitempty"`
}

// NextRun computes when s next fires, folding in this device's deterministic
// per-schedule jitter (daily/weekly/monthly only — see JitterOffset) so a
// fleet of devices sharing the same cadence doesn't wake in the same instant.
// deviceID is passed in explicitly rather than read from config/global state,
// so this stays reachable from anywhere NextRun is needed (runner ticks, the
// schedule.sync ack, tests) without threading a config dependency into this
// package.
func (s Schedule) NextRun(after time.Time, tz *time.Location, deviceID string) (time.Time, bool) {
	spec := s.Cadence
	spec.EndAt = s.EndAt // fold in the wire's top-level end_at before delegating
	return NextRunForDevice(spec, after, tz, deviceID, s.ID)
}

// storeFile is the on-disk shape of schedules.json. Timezone travels alongside
// the schedules (rather than living only in config.json — see the CRITICAL
// STORAGE RULE in the phase-5 brief) because every wall-clock cadence
// (daily/weekly/monthly) needs it on every runner tick, not just at sync time;
// keeping it here means the runner survives a device restart without waiting
// for a fresh schedule.sync.
type storeFile struct {
	Timezone  string     `json:"timezone,omitempty"`
	Schedules []Schedule `json:"schedules"`
}

// Store persists the device's schedule list to a JSON file (schedules.json,
// a SIBLING of config.json — never inside it: config_watch.go watches
// config.json for changes and reloads a good deal of device state on every
// edit, which a once-a-minute-or-more schedule write must not trigger).
//
// All access is serialized behind mu: the MQTT dispatch goroutine
// (schedule.sync / schedule.run) and the runner's own ticker goroutine both
// touch this concurrently.
type Store struct {
	mu   sync.Mutex
	path string
}

// NewStore returns a Store backed by path. Construction never touches disk —
// even a path whose directory doesn't exist yet is fine until the first write.
func NewStore(path string) *Store {
	return &Store{path: path}
}

// loadFileLocked reads schedules.json. A missing file (first boot — the
// backend hasn't pushed a schedule.sync yet) degrades to an empty struct, not
// an error. A corrupt/truncated file (e.g. a power loss that hit a WRITE
// somehow not covered by the tmp+rename below, or a hand-edited file) degrades
// the same way rather than panicking — the next schedule.sync from the
// backend is authoritative and will repair it.
func (s *Store) loadFileLocked() storeFile {
	data, err := os.ReadFile(s.path)
	if err != nil {
		return storeFile{}
	}
	var f storeFile
	if err := json.Unmarshal(data, &f); err != nil {
		return storeFile{}
	}
	return f
}

// saveFileLocked writes via a temp file in the SAME directory followed by an
// atomic rename, so a crash mid-write can never leave schedules.json
// truncated — a reader always sees either the complete old file or the
// complete new one, never a half-written one.
func (s *Store) saveFileLocked(f storeFile) error {
	dir := filepath.Dir(s.path)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return err
	}
	data, err := json.MarshalIndent(f, "", "  ")
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".schedules.*.tmp")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		os.Remove(tmpPath)
		return err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpPath)
		return err
	}
	if err := os.Rename(tmpPath, s.path); err != nil {
		os.Remove(tmpPath)
		return err
	}
	return nil
}

// Load returns the current schedule list. Never errors on a missing or
// corrupt file (see loadFileLocked) — both degrade to an empty list so a
// startup race or a half-crashed write can never crash-loop the device; the
// error return exists for a future on-disk format that can fail harder.
func (s *Store) Load() ([]Schedule, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.loadFileLocked().Schedules, nil
}

// Replace swaps in a brand-new schedule list, preserving whatever timezone is
// already on disk. The schedule.sync handler uses ReplaceWithTimezone instead,
// since it always has both together; Replace is for callers (and tests) that
// only care about the schedule list.
func (s *Store) Replace(schedules []Schedule) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	f := s.loadFileLocked()
	f.Schedules = schedules
	return s.saveFileLocked(f)
}

// ReplaceWithTimezone is Replace plus the device-wide timezone, written in the
// SAME atomic file write so a crash between "save the schedules" and "save the
// timezone" can never leave a mismatched pair on disk.
func (s *Store) ReplaceWithTimezone(schedules []Schedule, timezone string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.saveFileLocked(storeFile{Timezone: timezone, Schedules: schedules})
}

// Get returns one schedule by id.
func (s *Store) Get(id string) (Schedule, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, sch := range s.loadFileLocked().Schedules {
		if sch.ID == id {
			return sch, true
		}
	}
	return Schedule{}, false
}

// mutate loads the file, applies fn to the schedule matching id, and saves —
// all under one lock, so the read-modify-write is atomic with respect to other
// Store callers. A no-longer-existing id (e.g. a concurrent schedule.sync
// deleted it) is a silent no-op rather than an error: there is nothing left to
// update, and that's fine.
func (s *Store) mutate(id string, fn func(*Schedule)) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	f := s.loadFileLocked()
	for i := range f.Schedules {
		if f.Schedules[i].ID == id {
			fn(&f.Schedules[i])
			return s.saveFileLocked(f)
		}
	}
	return nil
}

// SetLastRun records the outcome of a run WITHOUT touching NextRunAt. This is
// what the manual "Run now" path (schedule.run) uses — the wire contract is
// explicit that running now must not perturb the schedule's regular cadence.
// The ticker's own automatic fires use RecordRunResult instead, which updates
// last-run bookkeeping AND the next occurrence together in one write.
func (s *Store) SetLastRun(id string, at time.Time, status string) error {
	return s.mutate(id, func(sch *Schedule) {
		sch.LastRunAt = at
		sch.LastRunStatus = status
	})
}

// RecordRunResult persists a scheduled (ticker-driven) run's outcome AND the
// freshly computed next occurrence in ONE atomic write. Doing both together
// matters for crash-safety: two separate writes could leave a schedule that
// looks "already ran" but is still due (fires again on the next tick) or vice
// versa (looks not-yet-run but its due time has already moved on) if the
// device lost power between them.
//
// Always called with a SUCCESS outcome by the runner (a failure uses
// SetLastFailedRun instead, which deliberately leaves NextRunAt alone — see
// its doc comment), so LastFailedOccurrence is cleared here: the occurrence
// that failure marker pointed at is now resolved.
func (s *Store) RecordRunResult(id string, at time.Time, status string, nextRunAt time.Time) error {
	return s.mutate(id, func(sch *Schedule) {
		sch.LastRunAt = at
		sch.LastRunStatus = status
		sch.NextRunAt = nextRunAt
		sch.LastFailedOccurrence = time.Time{}
	})
}

// SetLastFailedRun records a FAILED attempt, pinning it to occurrence (the
// schedule's NextRunAt at the time of the attempt) via LastFailedOccurrence.
// Like SetLastRun, it never touches NextRunAt — a failure must not burn the
// occurrence (I5). Used only by the runner's automatic ticker path, which
// needs the occurrence pin to suppress a repeated failure ack for retries of
// the SAME occurrence (see runner.fire()); the manual "Run now" path keeps
// using plain SetLastRun since it has no such retry/suppression concept.
func (s *Store) SetLastFailedRun(id string, at time.Time, occurrence time.Time) error {
	return s.mutate(id, func(sch *Schedule) {
		sch.LastRunAt = at
		sch.LastRunStatus = "failure"
		sch.LastFailedOccurrence = occurrence
	})
}

// SetNextRun updates only NextRunAt. Used by schedule.sync (seeding the
// freshly computed next run for every schedule right after a replace) and by
// the runner's stale-catch-up re-anchor (skipping a missed run forward without
// recording it as having actually run).
func (s *Store) SetNextRun(id string, at time.Time) error {
	return s.mutate(id, func(sch *Schedule) {
		sch.NextRunAt = at
	})
}

// Timezone returns the device-wide IANA zone last set by schedule.sync,
// resolved to a *time.Location. An empty, missing, or unresolvable zone falls
// back to UTC so a bad/absent value degrades to a consistent (if unlocalized)
// schedule instead of crashing the runner.
func (s *Store) Timezone() *time.Location {
	s.mu.Lock()
	name := s.loadFileLocked().Timezone
	s.mu.Unlock()
	if name == "" {
		return time.UTC
	}
	loc, err := time.LoadLocation(name)
	if err != nil {
		return time.UTC
	}
	return loc
}
