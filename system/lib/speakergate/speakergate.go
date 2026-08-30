// Package speakergate decides when buffered sensing events may be replayed to
// the agent.
//
// The problem it solves: an agent turn is marked idle as soon as the runtime
// has produced its reply text, but that reply is only then handed to HAL's TTS
// queue and can keep playing for tens of seconds. Every runtime drains its
// pending sensing events on that idle edge, so a passive event queued during
// the turn (typically presence.enter) starts a NEW turn while the previous
// answer is still coming out of the speaker. HAL's queue gives the speaker to
// the newest turn — by design, that is what makes barge-in work — so the reply
// the user actually asked for is cut off mid-sentence (device-observed, 28/8).
//
// The fix belongs here rather than in HAL: the preemption rule is correct, the
// turn was simply created too early. Six runtimes share the drain logic, so
// the rule lives in one predicate they all call.
package speakergate

import (
	"log/slog"
	"sync/atomic"
	"time"

	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/safego"
)

// pollInterval is how often the speaker is re-checked while a replay waits.
var pollInterval = 500 * time.Millisecond

// speakerBusy is the live speaker probe, indirected so the gate's own tests
// do not need a HAL on the loopback.
var speakerBusy = hal.SpeakerBusy

// maxWait caps the deferral. A stuck `speaking` flag on HAL must delay the
// replay, never cancel it: past maxWait the events are replayed anyway, which
// is the pre-existing behaviour.
var maxWait = 90 * time.Second

// deferring guards against stacking one waiter per drain call. The retry is a
// full drain of the same queue, so a second waiter would replay nothing extra
// and only multiply the polling.
var deferring atomic.Bool

// WaitsForSpeaker reports whether replaying eventType has to wait for the
// speaker to go idle.
//
// Only two kinds of event are exempt. User-driven ones — speech and chat —
// because a person talking over the device IS the barge-in the preemption rule
// exists for; making them wait would break interrupting a long answer. And
// fire_hazard.detected, because a safety alert that waits politely for a
// hunting-season answer to finish is worse than a cut-off reply.
func WaitsForSpeaker(eventType string) bool {
	switch eventType {
	case "voice", "voice_command", "voice_followup", "voice_agent_handled",
		"web_chat", "mqtt_chat", "fire_hazard.detected":
		return false
	default:
		return true
	}
}

// DeferReplay reports whether a drain of eventTypes must be postponed because
// the device is still speaking. When it returns true the caller re-queues its
// events untouched and returns; retry is invoked once the speaker frees up (or
// after maxWait).
//
// It returns false — replay now — as soon as ONE event in the batch is exempt:
// that event has to go through immediately, and holding back the rest of the
// batch would reorder the queue behind it.
func DeferReplay(eventTypes []string, retry func()) bool {
	if !speakerBusy() {
		return false
	}
	for _, t := range eventTypes {
		if !WaitsForSpeaker(t) {
			return false
		}
	}
	if !deferring.CompareAndSwap(false, true) {
		// Another waiter is already polling; it will drain this batch too.
		return true
	}
	slog.Info("sensing replay deferred -- device still speaking",
		"component", "sensing", "events", len(eventTypes))
	safego.Go("speakergate", func() {
		// The flag is released BEFORE retry runs, never in a defer: retry is a
		// full drain, and a drain re-entering DeferReplay must be able to open
		// a fresh waiter. Losing that CAS would return "deferred" with nobody
		// polling, stranding the queue until the next agent turn ends.
		defer deferring.Store(false)
		deadline := time.Now().Add(maxWait)
		for time.Now().Before(deadline) {
			time.Sleep(pollInterval)
			if !speakerBusy() {
				slog.Info("sensing replay resumed -- speaker idle", "component", "sensing")
				deferring.Store(false)
				retry()
				return
			}
		}
		slog.Warn("sensing replay resumed -- speaker still busy after max wait",
			"component", "sensing", "max_wait_s", int(maxWait.Seconds()))
		deferring.Store(false)
		retry()
	})
	return true
}
