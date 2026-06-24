package openclaw

import (
	"log/slog"
	"sort"
	"strings"
	"time"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/lib/flow"
	"go.autonomous.ai/os/lib/mood"
	"go.autonomous.ai/os/lib/sensingmsg"
)

// pendingEvent is a sensing event buffered while the agent was busy.
type pendingEvent struct {
	eventType   string
	msg         string
	image       string
	queuedAt    time.Time
	currentUser string // snapshot at queue time — may differ from replay time
	fixedRunID  string // preallocated runID (web_chat); empty = allocate at drain
}

// busyTTL bounds how long activeTurn can stay true without a clearing
// lifecycle.end. OpenClaw can drop the lifecycle.end SSE for heartbeat
// (target=none) turns and for concurrent same-lane sends that get merged
// into the active run; without this expiry, every sensing event after such
// a stuck turn is dropped/queued forever.
const busyTTL = 5 * time.Minute

// IsBusy returns true while the agent is processing a turn (between lifecycle
// start and end) OR has at least one chat.send still waiting for its
// lifecycle_start. The pending-send check closes the gap between Lamp's WS
// write and the agent echoing lifecycle_start back: during that gap
// activeTurn can briefly read false (if a previous turn's lifecycle_end just
// fired), letting new sensing slip through to OpenClaw direct and stack up
// behind already-in-flight turns in OpenClaw's per-session queue.
//
// Auto-clears activeTurn after busyTTL since the last SetBusy(true) so a
// dropped lifecycle.end cannot wedge the sensing pipeline indefinitely; even
// after that, fresh pending sends still keep IsBusy true.
func (s *OpenclawService) IsBusy() bool {
	if s.activeTurn.Load() {
		since := s.busySince.Load()
		if since > 0 && time.Since(time.UnixMilli(since)) > busyTTL {
			slog.Warn("busy flag expired — auto-clearing (lifecycle.end likely missed)",
				"component", "openclaw", "stuck_for_s", int(time.Since(time.UnixMilli(since)).Seconds()))
			s.activeTurn.Store(false)
			go s.drainPendingEvents()
			return s.HasFreshPendingChatSend()
		}
		return true
	}
	return s.HasFreshPendingChatSend()
}

// SetBusy marks the agent as busy or idle. Called by the SSE handler on lifecycle start/end.
// When transitioning to idle, any buffered sensing events are replayed.
func (s *OpenclawService) SetBusy(busy bool) {
	if busy {
		s.busySince.Store(time.Now().UnixMilli())
	}
	s.activeTurn.Store(busy)
	if !busy {
		s.drainPendingEvents()
	}
}

// QueuePendingEvent buffers a sensing event to replay when the agent becomes idle.
// All events are appended — motion/presence must not be missed.
func (s *OpenclawService) QueuePendingEvent(eventType, msg, image, fixedRunID string) {
	now := time.Now()
	curUser := mood.CurrentUser()
	if curUser == "" {
		curUser = "unknown"
	}
	s.pendingEventsMu.Lock()
	s.pendingEvents = append(s.pendingEvents, pendingEvent{eventType: eventType, msg: msg, image: image, queuedAt: now, currentUser: curUser, fixedRunID: fixedRunID})
	s.pendingEventsMu.Unlock()
	slog.Info("sensing event queued — agent busy", "component", "sensing", "type", eventType, "runId", fixedRunID)

	// Surface the queued event in the monitor immediately so the UI doesn't
	// look idle while the agent is busy. The original sensing_input flow
	// entry will fire later at drain time with queued_for_ms attached.
	s.monitorBus.Push(domain.MonitorEvent{
		Type:    "sensing_queued",
		Summary: "[" + eventType + "] " + msg,
		Detail:  map[string]any{"type": eventType, "reason": "agent_busy"},
	})
}

// drainPendingEvents replays all buffered sensing events in order and clears the buffer.
func (s *OpenclawService) drainPendingEvents() {
	s.pendingEventsMu.Lock()
	events := s.pendingEvents
	s.pendingEvents = nil
	s.pendingEventsMu.Unlock()

	if len(events) == 0 {
		return
	}

	// Prioritize voice events so user replies are processed before queued sensing events.
	sort.SliceStable(events, func(i, j int) bool {
		iv := events[i].eventType == "voice" || events[i].eventType == "voice_command"
		jv := events[j].eventType == "voice" || events[j].eventType == "voice_command"
		return iv && !jv
	})

	// Expire stale high-frequency events — replaying stale sensor signals just
	// floods OpenClaw's queue with no-longer-relevant turns (each ~15-20s LLM
	// call). Voice events are never expired because they carry user intent.
	// presence.enter/leave are time-sensitive too: if a person came in 60s ago,
	// the agent reacting now is awkward and the situation may have changed.
	const expireAfter = 60 * time.Second
	expirable := map[string]bool{
		"motion.activity":         true,
		"emotion.detected":        true,
		"speech_emotion.detected": true,
		"presence.enter":          true,
		"presence.leave":          true,
		"presence.away":           true,
	}
	filtered := events[:0]
	for _, ev := range events {
		if expirable[ev.eventType] && time.Since(ev.queuedAt) > expireAfter {
			slog.Info("sensing event expired from queue", "component", "sensing", "type", ev.eventType, "age_s", int(time.Since(ev.queuedAt).Seconds()))
			continue
		}
		filtered = append(filtered, ev)
	}
	events = filtered

	// Coalesce duplicates: for high-frequency sensor events, keep only the
	// latest of each type. Replaying every queued presence.enter / motion /
	// emotion produces a back-to-back chat.send burst that re-floods the
	// OpenClaw queue (the issue this whole gatekeeper exists to prevent).
	// Voice/voice_command keep all entries — each is a distinct user utterance.
	coalesce := map[string]bool{
		"presence.enter":          true,
		"presence.leave":          true,
		"presence.away":           true,
		"motion.activity":         true,
		"emotion.detected":        true,
		"speech_emotion.detected": true,
	}
	lastIdx := make(map[string]int, len(events))
	for i, ev := range events {
		if coalesce[ev.eventType] {
			lastIdx[ev.eventType] = i
		}
	}
	if len(lastIdx) > 0 {
		dropped := 0
		coalesced := events[:0]
		for i, ev := range events {
			if coalesce[ev.eventType] && lastIdx[ev.eventType] != i {
				dropped++
				continue
			}
			coalesced = append(coalesced, ev)
		}
		if dropped > 0 {
			slog.Info("sensing events coalesced — kept latest only", "component", "sensing", "dropped", dropped, "remaining", len(coalesced))
		}
		events = coalesced
	}

	if len(events) == 0 {
		slog.Info("all pending sensing events expired, nothing to drain", "component", "sensing")
		return
	}

	slog.Info("draining pending sensing events", "component", "sensing", "count", len(events))
	for _, ev := range events {
		// Allocate a dedicated run ID so each replayed event gets its own
		// sensing_input flow entry — required for the UI to render the turn.
		// web_chat preallocates the runID at queue time (already marked via
		// MarkWebChatRun) so the web client correlates its pending message;
		// reuse it instead of generating a new one.
		var reqID, runID string
		if ev.fixedRunID != "" {
			reqID = ev.fixedRunID
			runID = ev.fixedRunID
		} else {
			reqID, runID = s.NextChatRunID()
		}
		flow.SetTrace(runID)
		startPayload := map[string]any{"type": ev.eventType, "message": ev.msg}
		if !ev.queuedAt.IsZero() {
			startPayload["queued_for_ms"] = time.Since(ev.queuedAt).Milliseconds()
			startPayload["queued_at"] = ev.queuedAt.Unix()
		}
		turnStart := flow.Start("sensing_input", startPayload, runID)

		// Re-stash any pose bucket info riding on a motion.activity that was
		// queued while the agent was busy. Without this, the SSE /dm path
		// has no bucket to attach images from when the agent eventually
		// nudges on the replayed run. Matches the live sensing handler.
		if ev.eventType == "motion.activity" {
			if bid, worst := extractPoseBucketMarkers(ev.msg); bid != "" {
				s.MarkPoseBucketRun(runID, bid, worst)
			}
		}
		// Build the outgoing message via the shared helper so the drain path
		// stays identical to the live sensing handler. Guard tag is always ""
		// here — guard state isn't preserved across the queue.
		msg := sensingmsg.Build(ev.eventType, ev.msg, ev.currentUser, "")
		// Strip [snapshot: ...] + pose bucket markers from the outgoing LLM
		// message — matches the behaviour of the direct PostEvent path. The
		// full text still reaches the sensing_input JSONL via startPayload
		// above, so Monitor UI thumbnails + bucket popup keep working.
		msg = reSnapshotPath.ReplaceAllString(msg, "")
		msg = rePoseBucketMarker.ReplaceAllString(msg, "")
		msg = rePoseWorstMarker.ReplaceAllString(msg, "")
		msg = strings.ReplaceAll(msg, "\n\n\n", "\n\n")
		msg = strings.TrimSpace(msg)

		// Replayed voice_agent_handled: realtime agent already spoke, suppress TTS
		// on the reply (same as the live PostEvent path).
		if ev.eventType == "voice_agent_handled" {
			s.MarkSilentRun(runID)
		}

		var err error
		if ev.image != "" {
			_, err = s.SendChatMessageWithImageAndRun(msg, ev.image, reqID, runID)
		} else {
			_, err = s.SendChatMessageWithRun(msg, reqID, runID)
		}
		if err != nil {
			slog.Error("failed to replay pending event", "component", "sensing", "type", ev.eventType, "error", err)
			flow.End("sensing_input", turnStart, map[string]any{"error": err.Error()}, runID)
		} else {
			flow.End("sensing_input", turnStart, map[string]any{"path": "agent", "run_id": runID}, runID)
			flow.Log("agent_call", map[string]any{"type": ev.eventType, "run_id": runID}, runID)
			slog.Info("pending event replayed", "component", "sensing", "type", ev.eventType, "runId", runID)
		}
	}
}
