package schedule

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"go.autonomous.ai/os/system/domain"
)

const (
	// runnerTickInterval mirrors the connector-refresh loop's cadence class:
	// frequent enough that a schedule fires within a minute of its due time,
	// cheap because the common case (nothing due) is a fast no-op scan.
	runnerTickInterval = 1 * time.Minute

	// runnerCatchUpWindow bounds how overdue a schedule may be before the
	// runner gives up firing it and just re-anchors it forward instead. A
	// device that was powered off overnight must not fire every missed daily
	// briefing back-to-back the moment it boots — only a run that was due
	// within this window still fires (once); anything staler is silently
	// skipped forward to its next regular occurrence.
	runnerCatchUpWindow = 30 * time.Minute
)

// RunReport is handed to the report callback after every fire attempt
// (success or failure), whether it came from the ticker or from a manual
// "Run now". The MQTT layer (schedule_sync_handler.go / schedule_run_handler.go)
// turns this into the fd_channel schedule.run ack; the Runner itself knows
// nothing about MQTT.
type RunReport struct {
	ScheduleID string
	// RunID is the run id domain.AgentGateway.SendSystemChatMessage itself
	// returned — empty on a failed send, since no run ever started. This is
	// deliberately NOT a locally fabricated id: SendSystemChatMessage is
	// fire-and-forget and its return value IS the id Flow Monitor/the backend
	// can actually correlate against (domain/agent.go's doc comment). An
	// earlier revision fabricated its own id here and threw the real one away
	// into Summary instead — CRITICAL-3 from the phase-5 review.
	RunID     string
	StartedAt time.Time
	// SendLatency is how long the call to hand the message to the runtime
	// took — NOT how long the agent's turn took. SendSystemChatMessage returns
	// as soon as the message is sent (fire-and-forget), so this is send
	// latency, typically single-digit milliseconds, never turn duration.
	SendLatency time.Duration
	Status      string // "success" | "failure"
	Summary     string // the schedule's Name on success; the error text on failure
	// NextRunAt is the freshly computed next occurrence, set by fire() right
	// after it persists the same value via RecordRunResult (zero value
	// otherwise: a failed fire never advances NextRunAt, see fire()'s I5
	// comment, and RunNow deliberately never touches it either).
	//
	// CRITICAL FIX (final review): before this field existed, the schedule.run
	// ack never carried the device's newly computed next-fire time at all —
	// the only other writer of next_run_at is a schedule.sync ack, which only
	// happens on a user edit or while a row is still "pending". So a schedule
	// showed "in 16 hours" until its first fire, then "now" PERMANENTLY for
	// the rest of its life (the web UI's formatCadence.ts treats any
	// non-positive diff as "now") — the feature's headline column, broken
	// forever after the very first run.
	NextRunAt time.Time
}

// Runner is the on-device scheduler loop: once a minute it asks the Store
// which schedules are due and fires them through domain.AgentGateway's
// SendSystemChatMessage — the one method all six agentic runtimes implement,
// which is why the scheduler lives here instead of inside any one of them.
type Runner struct {
	store    *Store
	gw       domain.AgentGateway
	deviceID string
	report   func(RunReport)
}

// NewRunner builds a Runner. deviceID is threaded in explicitly (rather than
// read from config inside this package) purely so NextRun's jitter has
// somewhere to get it from without this package reaching into global state —
// see JitterOffset. report may be nil (tests that don't care about acks).
func NewRunner(store *Store, gw domain.AgentGateway, deviceID string, report func(RunReport)) *Runner {
	return &Runner{store: store, gw: gw, deviceID: deviceID, report: report}
}

// Start runs the ticker loop until ctx is cancelled. The first tick fires
// immediately (boot catch-up: schedules that came due while the device was
// off get evaluated right away instead of waiting up to a full tick
// interval), then it settles into the regular cadence.
func (r *Runner) Start(ctx context.Context) {
	r.safeTick(time.Now())
	ticker := time.NewTicker(runnerTickInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			r.safeTick(now)
		}
	}
}

// safeTick recovers from a panic in a single tick so one bad schedule (a nil
// pointer somewhere, a malformed Cadence that slipped past validation, ...)
// can never kill the whole loop — mirrors StartConnectorRefreshLoop's
// per-tick recovery.
func (r *Runner) safeTick(now time.Time) {
	defer func() {
		if rec := recover(); rec != nil {
			slog.Error("schedule: panic in runner tick", "component", "schedule", "panic", rec)
		}
	}()
	r.tick(now)
}

// tick evaluates every schedule once. Due schedules are fired one at a time,
// in order — never concurrently — and IsBusy() is checked immediately before
// each one, so if firing the first schedule puts the agent into a turn, a
// second schedule due in the very same tick is deferred rather than firing on
// top of it. It stays due and gets its turn on a later tick once the agent
// frees up (see the SingleFlight test).
func (r *Runner) tick(now time.Time) {
	schedules, err := r.store.Load()
	if err != nil {
		slog.Error("schedule: load failed", "component", "schedule", "error", err)
		return
	}
	tz := r.store.Timezone()

	for _, sch := range schedules {
		if !sch.Enabled || sch.NextRunAt.IsZero() || sch.NextRunAt.After(now) {
			continue // disabled, never computed, or simply not due yet
		}

		if overdue := now.Sub(sch.NextRunAt); overdue > runnerCatchUpWindow {
			r.reanchor(sch, now, tz)
			continue
		}

		if r.gw.IsBusy() {
			// Defer, don't interrupt: NextRunAt is left untouched so this same
			// schedule is re-evaluated (and, catch-up window permitting, fired)
			// on the next tick.
			continue
		}

		r.fire(sch, tz)
	}
}

// reanchor skips a too-stale-to-fire schedule forward to its next regular
// occurrence from `now`, without recording a run — it never actually ran, so
// LastRunAt/LastRunStatus are untouched.
func (r *Runner) reanchor(sch Schedule, now time.Time, tz *time.Location) {
	next, ok := sch.NextRun(now, tz, r.deviceID)
	if !ok {
		next = time.Time{} // spent (a "once"/end_at case) — leave it un-due
	}
	if err := r.store.SetNextRun(sch.ID, next); err != nil {
		slog.Error("schedule: reanchor failed", "component", "schedule", "schedule_id", sch.ID, "error", err)
	}
}

// fire sends sch's instructions through the gateway. On success it persists
// both the run outcome and the freshly computed next occurrence in one atomic
// store write; on failure it records the outcome WITHOUT touching NextRunAt
// (see the I5 comment below).
//
// The next occurrence is anchored on sch.NextRunAt (the due time just fired),
// not on `now` — using `now` would compress or stretch the cadence gap by
// however late this particular tick happened to run. But sch.NextRunAt is
// itself a JITTERED value (see JitterOffset), and NextRun expects an
// unjittered anchor — feeding the jittered value straight back in can return
// the SAME occurrence again, so the schedule never advances (CRITICAL-2 from
// the phase-5 review). DejitterAnchor reverses that shift first.
//
// Ack suppression (phase-5 review, round 2): I5's retry means a persistently
// failing send (agent WebSocket down, MQTT fd_channel up — the two are
// independent transports, so this is a real, reachable state, not a
// theoretical one) gets attempted on every tick for up to the 30-minute
// catch-up window — roughly 31 attempts for ONE missed occurrence. The RETRY
// is correct and must keep happening every tick. But each retry used to also
// invoke the report callback, and the backend writes one schedule_run history
// row PER ack — so one missed briefing became "failed 31 times". A failure is
// now ack'd only the FIRST time for a given occurrence (tracked via
// LastFailedOccurrence — see its doc comment); a SUCCESS always acks,
// including a success that follows earlier suppressed failures in the same
// occurrence, since that is the outcome the backend actually needs to hear.
func (r *Runner) fire(sch Schedule, tz *time.Location) {
	attemptID := fmt.Sprintf("sched-%s-%d", sch.ID, time.Now().UnixMilli())
	rr := r.send(sch, attemptID)

	if rr.Status != "success" {
		alreadyAckedThisOccurrence := sch.LastRunStatus == "failure" && sch.LastFailedOccurrence.Equal(sch.NextRunAt)

		// I5: a send failure must NOT burn the occurrence by advancing
		// NextRunAt. Leaving it untouched keeps the schedule "due" so it is
		// retried on the next tick(s) — bounded by the same 30-minute
		// catch-up window tick() already enforces (past that, tick()
		// re-anchors forward instead of calling fire() at all). Advancing
		// NextRunAt here would mean e.g. a WS reconnect exactly at 08:00
		// permanently loses that day's run instead of retrying moments later.
		if err := r.store.SetLastFailedRun(sch.ID, rr.StartedAt, sch.NextRunAt); err != nil {
			slog.Error("schedule: persist failed run failed", "component", "schedule", "schedule_id", sch.ID, "error", err)
		}
		if !alreadyAckedThisOccurrence && r.report != nil {
			r.report(rr)
		}
		return
	}

	base := DejitterAnchor(sch.Cadence.Repeat, sch.NextRunAt, r.deviceID, sch.ID)
	next, ok := sch.NextRun(base, tz, r.deviceID)
	if !ok {
		next = time.Time{}
	}
	if err := r.store.RecordRunResult(sch.ID, rr.StartedAt, rr.Status, next); err != nil {
		slog.Error("schedule: persist run result failed", "component", "schedule", "schedule_id", sch.ID, "error", err)
	}
	// Forward the same occurrence onto the ack (see RunReport.NextRunAt's doc
	// comment) — the backend has no other reliable way to learn it.
	rr.NextRunAt = next
	if r.report != nil {
		r.report(rr) // always ack a success, even after earlier suppressed failures this occurrence
	}
}

// RunNow fires sch immediately for the "Run now" button (kind schedule.run).
// Unlike the ticker's fire(), it updates ONLY last-run bookkeeping — cadence
// and NextRunAt are left exactly as they were, matching the wire contract's
// note that running now must not perturb the schedule's regular cadence.
//
// ok=false means the run was deferred (the agent is busy) rather than
// attempted at all — the same single-flight rule the ticker follows applies
// here too, so a manual trigger can never barge in on an active turn.
func (r *Runner) RunNow(sch Schedule) (report RunReport, ok bool) {
	if r.gw.IsBusy() {
		return RunReport{}, false
	}
	attemptID := fmt.Sprintf("sched-run-%s-%d", sch.ID, time.Now().UnixMilli())
	rr := r.send(sch, attemptID)
	if err := r.store.SetLastRun(sch.ID, rr.StartedAt, rr.Status); err != nil {
		slog.Error("schedule: persist manual run failed", "component", "schedule", "schedule_id", sch.ID, "error", err)
	}
	// A manual "Run now" always acks — there is no retry/suppression concept
	// here (unlike fire()'s automatic ticker path): one request, one ack.
	if r.report != nil {
		r.report(rr)
	}
	return rr, true
}

// send performs the actual gateway call and classifies the outcome. It does
// NOT touch the store and does NOT invoke the report callback — fire() and
// RunNow() decide separately what to persist and whether/when to report,
// since the ticker's automatic retries must suppress repeated failure acks
// for the same occurrence (see fire()'s doc comment) while RunNow always acks.
//
// attemptID is a LOCAL correlation id used only for the "firing" log line
// before the send completes — it is NOT reported as RunReport.RunID. The
// actual run id comes from SendSystemChatMessage's own return value once the
// send completes (see RunReport.RunID's doc comment).
func (r *Runner) send(sch Schedule, attemptID string) RunReport {
	started := time.Now()
	slog.Info("schedule: firing", "component", "schedule", "schedule_id", sch.ID, "attempt_id", attemptID)

	runID, err := r.gw.SendSystemChatMessage(sch.Instructions)
	latency := time.Since(started)

	status, summary := "success", sch.Name
	if err != nil {
		status, summary = "failure", err.Error()
		slog.Error("schedule: fire failed", "component", "schedule", "schedule_id", sch.ID, "error", err)
	}

	return RunReport{ScheduleID: sch.ID, RunID: runID, StartedAt: started, SendLatency: latency, Status: status, Summary: summary}
}
