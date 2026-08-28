package schedule

import (
	"errors"
	"path/filepath"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
)

// fakeGateway embeds domain.AgentGateway so only the methods the Runner
// actually calls are real; any other call would panic on the nil embedded
// interface, which is fine because these tests never exercise them. Mirrors
// the fakeGateway pattern used elsewhere in this repo (e.g.
// system/device/channel_test.go).
type fakeGateway struct {
	domain.AgentGateway
	busy          bool
	setBusyOnSend bool // simulate the WS lifecycle marking the agent busy the instant a turn starts
	sendErr       error
	sent          []string

	// spoken records Speak() calls, kept SEPARATE from sent so a test can
	// prove not just that the right text went out but that it went out on the
	// right transport — an "agent" task landing in spoken (or vice versa)
	// would otherwise look identical to a pass.
	spoken   []string
	speakErr error
}

func (f *fakeGateway) IsBusy() bool { return f.busy }

func (f *fakeGateway) Speak(text string) error {
	f.spoken = append(f.spoken, text)
	if f.setBusyOnSend {
		f.busy = true
	}
	return f.speakErr
}

func (f *fakeGateway) SendSystemChatMessage(msg string) (string, error) {
	f.sent = append(f.sent, msg)
	if f.setBusyOnSend {
		f.busy = true
	}
	if f.sendErr != nil {
		return "", f.sendErr
	}
	return "ok", nil
}

func newTestStore(t *testing.T) *Store {
	t.Helper()
	return NewStore(filepath.Join(t.TempDir(), "schedules.json"))
}

// seedJitteredSchedule stores sch (and any others already in schedules) via
// SyncSchedules — the SAME path a real schedule.sync uses — so NextRunAt comes
// back with this device's real jitter applied, exactly like production data.
// Deliberately NOT a hand-picked "clean" timestamp: a clean value is what let
// CRITICAL-2 (fire() feeding a jittered NextRunAt back into NextRun without
// reversing the jitter first) slip past the first review — a hand-set exact
// value can accidentally dodge that whole class of bug. Returns the resulting
// NextRunAt for the LAST schedule passed, for tests that seed one at a time.
func seedJitteredSchedule(t *testing.T, store *Store, computedFrom time.Time, deviceID string, schedules ...Schedule) time.Time {
	t.Helper()
	_, nextRunAt, err := SyncSchedules(store, schedules, "UTC", deviceID, computedFrom)
	if err != nil {
		t.Fatalf("SyncSchedules: %v", err)
	}
	last := schedules[len(schedules)-1]
	next, ok := nextRunAt[last.ID]
	if !ok {
		t.Fatalf("SyncSchedules did not compute next_run_at for %s", last.ID)
	}
	return next
}

func TestRunner_FiresDueScheduleViaSendSystemChatMessage(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Name: "Daily briefing", Instructions: "Say the daily briefing", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)
	now := scheduledAt.Add(time.Minute) // a normal, barely-overdue tick

	gw := &fakeGateway{}
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	r.tick(now)

	if len(gw.sent) != 1 || gw.sent[0] != sch.Instructions {
		t.Fatalf("sent = %v, want exactly [%q]", gw.sent, sch.Instructions)
	}
	if len(reports) != 1 || reports[0].Status != "success" || reports[0].ScheduleID != "s1" {
		t.Fatalf("reports = %+v", reports)
	}

	got, _ := store.Get("s1")
	if !got.NextRunAt.After(now) {
		t.Errorf("NextRunAt not advanced past the fire: %v", got.NextRunAt)
	}
	if got.LastRunStatus != "success" {
		t.Errorf("LastRunStatus = %q, want success", got.LastRunStatus)
	}
}

// Regression for CRITICAL-2 (final review): fire() computes and persists the
// next occurrence one statement before reporting, but that value used to
// never reach the RunReport handed to the report callback — so the
// schedule.run ack never carried a fresh next-fire time at all, and the web
// UI's cadence column got stuck on "now" permanently after a schedule's
// first run (formatCadence.ts treats any non-positive diff as "now").
// RunReport.NextRunAt must equal exactly what fire() just persisted to the
// store for the same fire.
func TestRunner_ReportsFreshlyComputedNextRunAt(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Name: "Daily briefing", Instructions: "Say the daily briefing", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)
	now := scheduledAt.Add(time.Minute)

	gw := &fakeGateway{}
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	r.tick(now)

	if len(reports) != 1 {
		t.Fatalf("reports = %+v, want exactly 1", reports)
	}
	if reports[0].NextRunAt.IsZero() {
		t.Fatal("RunReport.NextRunAt is zero — the freshly computed occurrence was never forwarded to the ack")
	}

	got, _ := store.Get("s1")
	if !reports[0].NextRunAt.Equal(got.NextRunAt) {
		t.Errorf("RunReport.NextRunAt = %v, want it to match the persisted NextRunAt %v", reports[0].NextRunAt, got.NextRunAt)
	}
}

// Regression for CRITICAL-3: RunReport.RunID must be the id
// SendSystemChatMessage itself returned (the fake gateway returns "ok"), NOT a
// locally fabricated "sched-<id>-<timestamp>" string — and Summary must be the
// schedule's name on success, never the run id.
func TestRunner_ReportsGatewayRunIDNotLocalID(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Name: "Daily briefing", Instructions: "hi", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)
	now := scheduledAt.Add(time.Minute)

	gw := &fakeGateway{} // SendSystemChatMessage returns "ok" as its run id
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	r.tick(now)

	if len(reports) != 1 {
		t.Fatalf("reports = %+v", reports)
	}
	got := reports[0]
	if got.RunID != "ok" {
		t.Errorf("RunID = %q, want the gateway's own returned run id (\"ok\"), not a locally fabricated one", got.RunID)
	}
	if got.Summary != "Daily briefing" {
		t.Errorf("Summary on success = %q, want the schedule name", got.Summary)
	}
}

// Regression for CRITICAL-3, failure branch: no run ever started, so RunID
// must be empty (mirroring what SendSystemChatMessage itself returns on
// error), and Summary must carry the actual error text.
func TestRunner_ReportsSendErrorAsSummaryWithEmptyRunID(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Name: "Daily briefing", Instructions: "hi", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)
	now := scheduledAt.Add(time.Minute)

	gw := &fakeGateway{sendErr: errors.New("ws disconnected")}
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	r.tick(now)

	if len(reports) != 1 {
		t.Fatalf("reports = %+v", reports)
	}
	got := reports[0]
	if got.Status != "failure" || got.Summary != "ws disconnected" {
		t.Errorf("got %+v", got)
	}
	if got.RunID != "" {
		t.Errorf("RunID on a failed send = %q, want empty (no run actually started)", got.RunID)
	}
}

// Regression for I5: a send failure must NOT advance NextRunAt — otherwise a
// transient failure (e.g. a WS reconnect exactly at the due time) permanently
// loses that occurrence instead of being retried on a later tick.
func TestRunner_SendFailureDoesNotAdvanceNextRunAt(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Instructions: "hi", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)
	now := scheduledAt.Add(time.Minute)

	gw := &fakeGateway{sendErr: errors.New("ws disconnected")}
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	r.tick(now)

	got, _ := store.Get("s1")
	if !got.NextRunAt.Equal(scheduledAt) {
		t.Errorf("NextRunAt changed after a failed send: got %v, want untouched %v", got.NextRunAt, scheduledAt)
	}
	if got.LastRunStatus != "failure" {
		t.Errorf("LastRunStatus = %q, want failure recorded even though NextRunAt was left alone", got.LastRunStatus)
	}
	if !got.LastFailedOccurrence.Equal(scheduledAt) {
		t.Errorf("LastFailedOccurrence = %v, want it pinned to the occurrence %v", got.LastFailedOccurrence, scheduledAt)
	}
	if len(reports) != 1 {
		t.Fatalf("the first failure of an occurrence must ack: reports = %+v", reports)
	}

	// Still within the catch-up window on the next tick -> retried, but the
	// retry's ack is suppressed (see the dedicated suppression tests below).
	r.tick(now.Add(time.Minute))
	if len(gw.sent) != 2 {
		t.Fatalf("sent = %v, want a retry attempt on the next tick", gw.sent)
	}
	if len(reports) != 1 {
		t.Fatalf("a retry of the SAME occurrence must not ack again: reports = %+v", reports)
	}
}

// IMPORTANT (phase-5 review, round 2): I5's retry means a persistently
// failing send (agent WebSocket down, MQTT fd_channel up — the two are
// independent transports, so this is genuinely reachable, not theoretical)
// gets attempted on every tick for the full 30-minute catch-up window —
// roughly 31 attempts for ONE missed occurrence. Each attempt used to also
// invoke the report callback, and the backend writes one schedule_run history
// row PER ack, so one missed briefing became "failed 31 times". The retries
// must keep happening (that part is correct); only ONE failure ack may be
// emitted per occurrence.
func TestRunner_SuppressesRepeatedFailureAcksWithinSameOccurrence(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Instructions: "hi", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)

	gw := &fakeGateway{sendErr: errors.New("ws disconnected")}
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })

	// Drive the ticker across the FULL 30-minute catch-up window: minute 0
	// through minute 31 inclusive (32 ticks). Minutes 0-30 (31 ticks) are
	// still within the window and each attempts a send; minute 31 is past it
	// and re-anchors instead of attempting anything.
	for i := 0; i <= 31; i++ {
		r.tick(scheduledAt.Add(time.Duration(i) * time.Minute))
	}

	if len(gw.sent) != 31 {
		t.Fatalf("sent = %d attempts, want exactly 31 (one per tick within the 30-minute catch-up window) — the retry behaviour must NOT change", len(gw.sent))
	}
	if len(reports) != 1 {
		t.Fatalf("reports = %+v (%d acks), want exactly 1 for this one occurrence", reports, len(reports))
	}
	if reports[0].Status != "failure" {
		t.Errorf("reports[0].Status = %q, want failure", reports[0].Status)
	}
}

// A success must always ack, even when it follows earlier suppressed
// failures in the same occurrence — that outcome is exactly what the backend
// needs to hear once the agent recovers.
func TestRunner_SuccessAfterSuppressedFailuresStillAcks(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Name: "Daily briefing", Instructions: "hi", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)

	gw := &fakeGateway{sendErr: errors.New("ws disconnected")}
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })

	// Two failed attempts within the same occurrence -> exactly one (failure) ack.
	r.tick(scheduledAt)
	r.tick(scheduledAt.Add(time.Minute))
	if len(reports) != 1 || reports[0].Status != "failure" {
		t.Fatalf("after 2 failures, reports = %+v, want exactly 1 failure ack", reports)
	}

	// Agent recovers -> the next attempt succeeds -> must ack despite the
	// earlier suppressed failure(s).
	gw.sendErr = nil
	r.tick(scheduledAt.Add(2 * time.Minute))
	if len(reports) != 2 || reports[1].Status != "success" {
		t.Fatalf("after recovery, reports = %+v, want a second (success) ack", reports)
	}
}

// The next occurrence must start clean: once a schedule ages past the
// catch-up window and is re-anchored, its stale LastFailedOccurrence marker
// no longer matches the NEW NextRunAt, so the next occurrence's first failure
// acks again rather than being wrongly suppressed by the previous one's
// already-ack'd marker.
func TestRunner_NextOccurrenceAcksItsFirstFailureAgain(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Instructions: "hi", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)

	gw := &fakeGateway{sendErr: errors.New("ws disconnected")}
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })

	// Exhaust the catch-up window for the FIRST occurrence: exactly one ack.
	for i := 0; i <= 31; i++ {
		r.tick(scheduledAt.Add(time.Duration(i) * time.Minute))
	}
	if len(reports) != 1 {
		t.Fatalf("first occurrence: reports = %+v, want exactly 1", reports)
	}

	// It has now been re-anchored to the next occurrence. Drive a tick right
	// at that new due time: its first failure must ack again.
	got, _ := store.Get("s1")
	if got.NextRunAt.IsZero() {
		t.Fatal("expected a re-anchored NextRunAt for the next occurrence")
	}
	r.tick(got.NextRunAt)

	if len(reports) != 2 {
		t.Fatalf("second occurrence's first failure was wrongly suppressed: reports = %+v, want 2 total", reports)
	}
	if reports[1].Status != "failure" {
		t.Errorf("reports[1].Status = %q, want failure", reports[1].Status)
	}
}

// Regression for CRITICAL-2: fire() used to feed the JITTERED NextRunAt
// straight back into NextRun, which (for roughly half of all (device,
// schedule) id pairs — whichever hash to a NEGATIVE offset) never advances:
// the schedule re-fires on every 1-minute tick until it ages past the
// 30-minute catch-up window. "device-3"/"s1" is used deliberately (not
// "device-1"): JitterOffset("device-3","s1") is confirmed negative
// (-2m47s at time of writing), which is exactly the case that triggered the
// bug — a positive-jitter id pair would pass even on the broken code and this
// test would be worthless as a regression guard. Seeding via SyncSchedules
// (not a hand-picked clean timestamp) is what actually produces a jittered
// value in the first place. Simulating 40 ticks across 40 simulated minutes
// must produce EXACTLY ONE send.
func TestRunner_DoesNotReFireWithinTheSameOccurrence(t *testing.T) {
	const deviceID = "device-3"
	if off := JitterOffset(deviceID, "s1"); off >= 0 {
		t.Fatalf("test fixture bug: JitterOffset(%q, \"s1\") = %v, want negative (that's the case this test must exercise)", deviceID, off)
	}

	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Instructions: "hi", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 30, 0, 0, time.UTC), deviceID, sch)

	gw := &fakeGateway{}
	r := NewRunner(store, gw, deviceID, nil)

	for i := 0; i < 40; i++ {
		r.tick(scheduledAt.Add(time.Duration(i) * time.Minute))
	}

	if len(gw.sent) != 1 {
		t.Fatalf("sent = %v (%d messages) across 40 simulated minutes, want exactly 1", gw.sent, len(gw.sent))
	}
}

func TestRunner_DefersWhenGatewayBusy(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Instructions: "hi", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)
	now := scheduledAt.Add(time.Minute)

	gw := &fakeGateway{busy: true}
	r := NewRunner(store, gw, "device-1", nil)
	r.tick(now)

	if len(gw.sent) != 0 {
		t.Fatalf("must not send while the gateway is busy: sent = %v", gw.sent)
	}
	got, _ := store.Get("s1")
	if !got.NextRunAt.Equal(scheduledAt) {
		t.Errorf("NextRunAt changed while deferred: got %v, want untouched %v", got.NextRunAt, scheduledAt)
	}
}

// 29 minutes overdue -> still within the catch-up window, fires exactly once.
func TestRunner_BootCatchUpFiresRecentlyOverdueOnce(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Instructions: "hi", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)
	now := scheduledAt.Add(29 * time.Minute)

	gw := &fakeGateway{}
	r := NewRunner(store, gw, "device-1", nil)
	r.tick(now)

	if len(gw.sent) != 1 {
		t.Fatalf("sent = %v, want exactly 1", gw.sent)
	}

	// A tick moments later must not fire it again — NextRunAt has moved on to
	// tomorrow.
	r.tick(now.Add(time.Minute))
	if len(gw.sent) != 1 {
		t.Fatalf("fired again on a later tick: sent = %v", gw.sent)
	}
}

// 31 minutes overdue -> past the catch-up window: skipped, not fired, and
// re-anchored forward so it doesn't queue up a burst of missed runs.
func TestRunner_BootCatchUpSkipsStaleOverdue(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Instructions: "hi", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)
	now := scheduledAt.Add(31 * time.Minute)

	gw := &fakeGateway{}
	r := NewRunner(store, gw, "device-1", nil)
	r.tick(now)

	if len(gw.sent) != 0 {
		t.Fatalf("must not fire a stale overdue run: sent = %v", gw.sent)
	}
	got, _ := store.Get("s1")
	if !got.NextRunAt.After(now) {
		t.Errorf("NextRunAt not re-anchored forward: got %v, want after %v", got.NextRunAt, now)
	}
	if got.LastRunStatus != "" {
		t.Errorf("a re-anchor must not record a run: LastRunStatus = %q", got.LastRunStatus)
	}
}

// Two schedules due in the same tick: the second must wait for the first
// rather than firing concurrently on top of it.
func TestRunner_SingleFlight(t *testing.T) {
	store := newTestStore(t)
	computedFrom := time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC)
	s1 := Schedule{ID: "s1", Instructions: "one", Enabled: true, Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"}}
	s2 := Schedule{ID: "s2", Instructions: "two", Enabled: true, Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"}}
	_, nextRunAt, err := SyncSchedules(store, []Schedule{s1, s2}, "UTC", "device-1", computedFrom)
	if err != nil {
		t.Fatalf("SyncSchedules: %v", err)
	}
	scheduledAt1, scheduledAt2 := nextRunAt["s1"], nextRunAt["s2"]
	// Drive both from whichever is later, so a tick sees both as due at once.
	now := scheduledAt1
	if scheduledAt2.After(now) {
		now = scheduledAt2
	}
	now = now.Add(time.Minute)

	gw := &fakeGateway{setBusyOnSend: true} // mimics the real WS lifecycle: busy flips true the moment a turn starts
	r := NewRunner(store, gw, "device-1", nil)
	r.tick(now)

	if len(gw.sent) != 1 || gw.sent[0] != "one" {
		t.Fatalf("sent = %v, want exactly [\"one\"] in this tick — the second schedule must wait, not run concurrently", gw.sent)
	}
	got2, _ := store.Get("s2")
	if !got2.NextRunAt.Equal(scheduledAt2) {
		t.Errorf("deferred schedule's NextRunAt must be untouched: got %v, want %v", got2.NextRunAt, scheduledAt2)
	}

	// Once the agent frees up, the deferred one gets its turn on a later tick.
	gw.busy = false
	r.tick(now.Add(time.Minute))
	if len(gw.sent) != 2 || gw.sent[1] != "two" {
		t.Fatalf("sent = %v, want the deferred schedule to run once the agent is free", gw.sent)
	}
}

func TestRunner_DisabledScheduleNeverFires(t *testing.T) {
	store := newTestStore(t)
	// Disabled schedules get no computed NextRunAt from SyncSchedules at all
	// (see its doc comment) — set one directly here to prove that even a
	// disabled schedule that LOOKS due must never fire.
	scheduledAt := time.Date(2026, 8, 26, 8, 0, 0, 0, time.UTC)
	now := scheduledAt.Add(time.Minute)

	if err := store.Replace([]Schedule{{
		ID: "s1", Instructions: "hi", Enabled: false,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"}, NextRunAt: scheduledAt,
	}}); err != nil {
		t.Fatalf("seed: %v", err)
	}

	gw := &fakeGateway{}
	r := NewRunner(store, gw, "device-1", nil)
	r.tick(now)

	if len(gw.sent) != 0 {
		t.Fatalf("a disabled schedule fired: sent = %v", gw.sent)
	}
}

func TestRunner_RunNowDefersWhenBusyAndDoesNotTouchNextRunAt(t *testing.T) {
	store := newTestStore(t)
	scheduledAt := time.Date(2026, 8, 27, 8, 0, 0, 0, time.UTC)
	if err := store.Replace([]Schedule{{
		ID: "s1", Name: "Daily briefing", Instructions: "hi", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"}, NextRunAt: scheduledAt,
	}}); err != nil {
		t.Fatalf("seed: %v", err)
	}
	sch, _ := store.Get("s1")

	busyGW := &fakeGateway{busy: true}
	r := NewRunner(store, busyGW, "device-1", nil)
	if _, ok := r.RunNow(sch); ok {
		t.Fatal("RunNow must defer (ok=false) while the gateway is busy")
	}
	if len(busyGW.sent) != 0 {
		t.Fatalf("must not send while busy: sent = %v", busyGW.sent)
	}

	freeGW := &fakeGateway{}
	var reports []RunReport
	r2 := NewRunner(store, freeGW, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	report, ok := r2.RunNow(sch)
	if !ok {
		t.Fatal("RunNow should have run against a free gateway")
	}
	if len(freeGW.sent) != 1 || freeGW.sent[0] != "hi" {
		t.Fatalf("sent = %v", freeGW.sent)
	}
	if len(reports) != 1 {
		t.Fatalf("reports = %+v", reports)
	}
	if report.RunID != "ok" {
		t.Errorf("RunID = %q, want the gateway's own returned run id", report.RunID)
	}
	if report.Summary != "Daily briefing" {
		t.Errorf("Summary = %q, want the schedule name", report.Summary)
	}

	got, _ := store.Get("s1")
	if !got.NextRunAt.Equal(scheduledAt) {
		t.Errorf("RunNow must not change NextRunAt: got %v, want untouched %v", got.NextRunAt, scheduledAt)
	}
	if got.LastRunStatus != "success" {
		t.Errorf("LastRunStatus = %q, want success", got.LastRunStatus)
	}
}
