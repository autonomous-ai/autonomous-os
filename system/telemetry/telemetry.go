// Package telemetry is the device-side pipe for product analytics events.
//
// It is deliberately generic: voice metrics is the first tracker, more will
// follow. A tracker builds an Event and calls Report; this package owns
// everything after that — common fields, de-duplication, a bounded queue, a
// single background sender, and the local log line that makes an event
// visible on the device even when the network is down.
//
// Two rules shape the design:
//
//   - Report never blocks its caller and never fails it. Voice/audio work
//     calls into this from its own critical path; a slow or dead uplink must
//     cost that path nothing.
//   - Every event is logged locally before it is sent, and delivery failures
//     are logged too and counted. Success rates in the warehouse must never
//     be inflated by events that silently never arrived — the counters ride
//     along on later events so the loss is visible there as well.
package telemetry

import (
	"context"
	"encoding/json"
	"log/slog"
	"sync"
	"sync/atomic"
	"time"

	"go.autonomous.ai/os/system/lib/analytics"
)

// Event is one telemetry observation. Params carry the tracker's own fields;
// common fields (version, runtime, counters) are added by this package.
type Event struct {
	// Name is the AA event_name, e.g. "voice_metrics_interaction".
	Name string
	// ID de-duplicates retries: the same ID is sent at most once. Producers
	// that can retry (HAL re-posting after a failed HTTP call) must set it.
	ID string
	// Params are the tracker's fields. Never put transcripts, reply text,
	// raw audio, or credentials in here.
	Params map[string]any
}

const (
	queueSize   = 256
	sendTimeout = 10 * time.Second
	// dedupeTTL bounds the seen-ID registry. Well past any producer retry
	// window; the entry only has to outlive the retries of its own event.
	dedupeTTL = 30 * time.Minute
	logPrefix = "[telemetry]"
)

// Enabled reports whether events may leave the device: true when the body's
// .env names an analytics endpoint. No endpoint configured = nothing is sent.
//
// OFF does not mean blind — every event is still written to the local log, so
// `journalctl -u os-server | grep '[telemetry]'` shows what WOULD have been
// sent. Only the network hop is skipped.
func Enabled() bool { return analytics.Endpoint() != "" }

// sendFunc is the transport signature (analytics.TrackEvent in production).
type sendFunc func(ctx context.Context, name string, params map[string]any) error

type reporter struct {
	queue chan Event
	// send is captured per reporter, not read from a package global: a test
	// that swaps the transport must not be reachable by a previous test's
	// still-draining worker.
	sender sendFunc

	startOnce sync.Once

	commonMu sync.RWMutex
	common   map[string]any

	seenMu sync.Mutex
	seen   map[string]time.Time

	dropped atomic.Int64 // queue full
	failed  atomic.Int64 // transport error
	deduped atomic.Int64
}

var global = newReporter(analytics.TrackEvent)

func newReporter(send sendFunc) *reporter {
	return &reporter{
		queue:  make(chan Event, queueSize),
		sender: send,
		common: map[string]any{},
		seen:   map[string]time.Time{},
	}
}

// SetCommon sets fields stamped on every event from now on (os version,
// agent runtime, policy flags). Called once at startup; later calls replace
// the whole set.
func SetCommon(fields map[string]any) {
	global.commonMu.Lock()
	global.common = fields
	global.commonMu.Unlock()
}

// Report queues one event. Non-blocking: a full queue drops the event, logs
// it, and bumps the dropped counter so the loss is reported rather than
// hidden. Safe to call before Start — the queue buffers until then.
func Report(ev Event) {
	if ev.Name == "" {
		return
	}
	global.startOnce.Do(func() { go global.run(context.Background()) })

	if global.isDuplicate(ev.ID) {
		global.deduped.Add(1)
		slog.Info(logPrefix+" duplicate event ignored", "component", "telemetry",
			"event_name", ev.Name, "event_id", ev.ID)
		return
	}

	// Log before queueing: this line is the device-local record of the event
	// and must exist whether or not it ever reaches the warehouse — or whether
	// sending is enabled at all.
	slog.Info(logPrefix+" event", "component", "telemetry",
		"event_name", ev.Name, "event_id", ev.ID, "params", compactJSON(ev.Params))

	if !Enabled() {
		slog.Debug(logPrefix+" not sent -- no analytics endpoint configured", "component", "telemetry",
			"event_name", ev.Name, "event_id", ev.ID)
		return
	}

	select {
	case global.queue <- ev:
	default:
		n := global.dropped.Add(1)
		slog.Warn(logPrefix+" event dropped -- queue full", "component", "telemetry",
			"event_name", ev.Name, "event_id", ev.ID, "dropped_total", n)
	}
}

// Stats reports delivery health: events dropped by a full queue, events the
// transport rejected, and duplicates suppressed.
func Stats() (dropped, failed, deduped int64) {
	return global.dropped.Load(), global.failed.Load(), global.deduped.Load()
}

func (r *reporter) isDuplicate(id string) bool {
	if id == "" {
		return false
	}
	now := time.Now()
	r.seenMu.Lock()
	defer r.seenMu.Unlock()
	if _, ok := r.seen[id]; ok {
		return true
	}
	cutoff := now.Add(-dedupeTTL)
	for k, ts := range r.seen {
		if ts.Before(cutoff) {
			delete(r.seen, k)
		}
	}
	r.seen[id] = now
	return false
}

func (r *reporter) run(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case ev := <-r.queue:
			r.send(ctx, ev)
		}
	}
}

func (r *reporter) send(ctx context.Context, ev Event) {
	params := make(map[string]any, len(ev.Params)+8)
	r.commonMu.RLock()
	for k, v := range r.common {
		params[k] = v
	}
	r.commonMu.RUnlock()
	for k, v := range ev.Params {
		params[k] = v
	}
	params["event_id"] = ev.ID
	// Coverage: how much this device has lost so far. A warehouse query that
	// sees these climb knows its success rate is computed on partial data.
	params["telemetry_dropped_total"] = r.dropped.Load()
	params["telemetry_failed_total"] = r.failed.Load()

	sendCtx, cancel := context.WithTimeout(ctx, sendTimeout)
	defer cancel()
	if err := r.sender(sendCtx, ev.Name, params); err != nil {
		n := r.failed.Add(1)
		// The event is already in the local log above; this line says it
		// never left the device, which is the part a warehouse query cannot
		// tell you.
		slog.Warn(logPrefix+" delivery failed", "component", "telemetry",
			"event_name", ev.Name, "event_id", ev.ID, "error", err, "failed_total", n)
		return
	}
	slog.Debug(logPrefix+" delivered", "component", "telemetry",
		"event_name", ev.Name, "event_id", ev.ID)
}

// compactJSON renders params for the local log line. Falls back to Go's own
// formatting rather than losing the line to an unmarshalable value.
func compactJSON(params map[string]any) string {
	b, err := json.Marshal(params)
	if err != nil {
		return "unserializable"
	}
	return string(b)
}
