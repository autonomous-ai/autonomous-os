package hermes

import (
	"fmt"
	"log/slog"
	"sort"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/lib/sensingmsg"
	"go.autonomous.ai/os/system/lib/speakergate"
	"go.autonomous.ai/os/system/skillcontext/mood"
)

// pendingEvent is a sensing event buffered while the agent was busy.
type pendingEvent struct {
	eventType   string
	msg         string
	image       string
	queuedAt    time.Time
	currentUser string
	fixedRunID  string
}

const busyTTL = 5 * time.Minute

// mergeDrainEnabled collapses multiple ambient sensing events that survive the
// drain filter into a single turn, so the agent pays the per-turn prompt floor
// once instead of once per event. Hermes' backend has no steer mode (unlike
// openclaw's messages.queue.mode=steer, which merges concurrent messages into
// the in-flight turn at the next model boundary), so we batch client-side on
// idle here. Set false to fall back to one-turn-per-event replay.
const mergeDrainEnabled = true

// mergedSensingHeader frames a batched drain so the agent treats the joined
// lines as combined context for a single response instead of separate commands.
const mergedSensingHeader = "[ambient signals batched while busy — respond once, using the items below as combined context]\n\n"

// standaloneDrain reports whether an event must keep its own turn rather than be
// merged: real voice commands (must be answered directly, never buried under
// ambient signals), voice_agent_handled (silent reply can't share a turn with
// events that should speak), and image-bearing events (a merged text turn can't
// carry multiple images cleanly; sensing snapshots are stripped anyway).
func standaloneDrain(ev pendingEvent) bool {
	if ev.image != "" {
		return true
	}
	switch ev.eventType {
	case "voice", "voice_command", "voice_agent_handled":
		return true
	}
	return false
}

// IsBusy mirrors openclaw.HermesService.IsBusy: true while a turn is in flight OR a
// chat.send is still waiting for response.created. Auto-clears after busyTTL
// if response.completed got dropped so the sensing pipeline cannot wedge.
func (s *HermesService) IsBusy() bool {
	if s.activeTurn.Load() {
		since := s.busySince.Load()
		if since > 0 && time.Since(time.UnixMilli(since)) > busyTTL {
			slog.Warn("busy flag expired — auto-clearing (response.completed likely missed)",
				"component", "hermes", "stuck_for_s", int(time.Since(time.UnixMilli(since)).Seconds()))
			s.activeTurn.Store(false)
			go s.drainPendingEvents()
			return s.HasFreshPendingChatSend()
		}
		return true
	}
	return s.HasFreshPendingChatSend()
}

// SetBusy flips active state. Drains pending events on idle.
func (s *HermesService) SetBusy(busy bool) {
	if busy {
		s.busySince.Store(time.Now().UnixMilli())
	}
	s.activeTurn.Store(busy)
	if !busy {
		s.drainPendingEvents()
	}
}

func (s *HermesService) QueuePendingEvent(eventType, msg, image, fixedRunID string) {
	now := time.Now()
	curUser := mood.CurrentUser()
	if curUser == "" {
		curUser = "unknown"
	}
	s.pendingEventsMu.Lock()
	s.pendingEvents = append(s.pendingEvents, pendingEvent{eventType: eventType, msg: msg, image: image, queuedAt: now, currentUser: curUser, fixedRunID: fixedRunID})
	s.pendingEventsMu.Unlock()
	slog.Info("sensing event queued — agent busy", "component", "sensing", "type", eventType, "runId", fixedRunID)

	s.monitorBus.Push(domain.MonitorEvent{
		Type:    "sensing_queued",
		Summary: "[" + eventType + "] " + msg,
		Detail:  map[string]any{"type": eventType, "reason": "agent_busy"},
	})
}

// drainPendingEvents replays buffered sensing events. Behaviour matches the
// openclaw drain: voice events prioritised, expirable high-frequency types
// (presence / motion / emotion) coalesced to latest-only and stale entries
// dropped after expireAfter.
func (s *HermesService) drainPendingEvents() {
	s.pendingEventsMu.Lock()
	events := s.pendingEvents
	s.pendingEvents = nil
	s.pendingEventsMu.Unlock()

	if len(events) == 0 {
		return
	}

	// The turn that just ended may still be coming out of the speaker: a
	// runtime goes idle when the reply text is queued for TTS, not when it has
	// been spoken. Replaying a passive event now would open a newer turn and
	// HAL would hand it the speaker mid-sentence, cutting the answer the user
	// asked for. Put the batch back and let speakergate call us again.
	replayTypes := make([]string, len(events))
	for i, ev := range events {
		replayTypes[i] = ev.eventType
	}
	if speakergate.DeferReplay(replayTypes, s.drainPendingEvents) {
		s.pendingEventsMu.Lock()
		s.pendingEvents = append(events, s.pendingEvents...)
		s.pendingEventsMu.Unlock()
		return
	}

	sort.SliceStable(events, func(i, j int) bool {
		iv := events[i].eventType == "voice" || events[i].eventType == "voice_command"
		jv := events[j].eventType == "voice" || events[j].eventType == "voice_command"
		return iv && !jv
	})

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

	if !mergeDrainEnabled {
		for _, ev := range events {
			s.sendOnePending(ev)
		}
		return
	}

	// Partition: standalone events (voice commands, silent replays, images) keep
	// their own turn; the rest are pure-ambient sensing collapsed into one turn.
	var mergeable []pendingEvent
	for _, ev := range events {
		if standaloneDrain(ev) {
			s.sendOnePending(ev)
			continue
		}
		mergeable = append(mergeable, ev)
	}
	switch len(mergeable) {
	case 0:
		// nothing mergeable
	case 1:
		s.sendOnePending(mergeable[0]) // single event — merging is pointless
	default:
		s.sendMergedPending(mergeable)
	}
}

// sendOnePending replays a single buffered event as its own turn, preserving the
// original per-event run tracing, pose-bucket marking, silent-run marking and
// image attachment.
func (s *HermesService) sendOnePending(ev pendingEvent) {
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

	if ev.eventType == "motion.activity" {
		if bid, worst := extractPoseBucketMarkers(ev.msg); bid != "" {
			s.MarkPoseBucketRun(runID, bid, worst)
		}
	}
	msg := sensingmsg.Build(ev.eventType, ev.msg, ev.currentUser, "")
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
		return
	}
	flow.End("sensing_input", turnStart, map[string]any{"path": "agent", "run_id": runID}, runID)
	flow.Log("agent_call", map[string]any{"type": ev.eventType, "run_id": runID}, runID)
	slog.Info("pending event replayed", "component", "sensing", "type", ev.eventType, "runId", runID)
}

// sendMergedPending collapses multiple ambient sensing events into a single turn:
// each event's message is built through the same pipeline as sendOnePending, then
// joined under one runID and sent as one chat. The agent sees all signals at once
// (so it can respond coherently) and the prompt floor is paid only once. Called
// only when 2+ mergeable events survive the drain filter.
func (s *HermesService) sendMergedPending(evs []pendingEvent) {
	reqID, runID := s.NextChatRunID()
	flow.SetTrace(runID)

	// Pose-bucket markers must be registered against the merged runID before the
	// markers are stripped from the message text below.
	for _, ev := range evs {
		if ev.eventType == "motion.activity" {
			if bid, worst := extractPoseBucketMarkers(ev.msg); bid != "" {
				s.MarkPoseBucketRun(runID, bid, worst)
			}
		}
	}

	merged, types, oldest := buildMergedSensing(evs)
	if merged == "" {
		return
	}
	count := len(types)

	startPayload := map[string]any{"types": types, "merged": true, "count": count}
	if !oldest.IsZero() {
		startPayload["queued_for_ms"] = time.Since(oldest).Milliseconds()
		startPayload["queued_at"] = oldest.Unix()
	}
	turnStart := flow.Start("sensing_input", startPayload, runID)

	s.monitorBus.Push(domain.MonitorEvent{
		Type:    "sensing_drain_merged",
		Summary: fmt.Sprintf("%d sensing events → 1 turn", count),
		Detail:  map[string]any{"types": types, "count": count},
		RunID:   runID,
	})

	if _, err := s.SendChatMessageWithRun(merged, reqID, runID); err != nil {
		slog.Error("failed to replay merged sensing events", "component", "sensing", "types", types, "error", err)
		flow.End("sensing_input", turnStart, map[string]any{"error": err.Error()}, runID)
		return
	}
	flow.End("sensing_input", turnStart, map[string]any{"path": "agent", "run_id": runID, "merged": count}, runID)
	flow.Log("agent_call", map[string]any{"types": types, "run_id": runID, "merged": count}, runID)
	slog.Info("merged sensing events replayed", "component", "sensing", "types", types, "count", count, "runId", runID)
}

// buildMergedSensing builds the merged chat message from ambient sensing events.
// Pure (no side effects) so it can be unit-tested without the network. Each event
// is rendered through the same sensingmsg.Build + marker-strip pipeline as the
// single-event path, then joined under one header. types and oldest cover only
// events that produced a non-empty line, so count == merged-line count.
func buildMergedSensing(evs []pendingEvent) (merged string, types []string, oldest time.Time) {
	types = make([]string, 0, len(evs))
	parts := make([]string, 0, len(evs))
	for _, ev := range evs {
		m := sensingmsg.Build(ev.eventType, ev.msg, ev.currentUser, "")
		m = reSnapshotPath.ReplaceAllString(m, "")
		m = rePoseBucketMarker.ReplaceAllString(m, "")
		m = rePoseWorstMarker.ReplaceAllString(m, "")
		m = strings.TrimSpace(m)
		if m == "" {
			continue
		}
		parts = append(parts, m)
		types = append(types, ev.eventType)
		if !ev.queuedAt.IsZero() && (oldest.IsZero() || ev.queuedAt.Before(oldest)) {
			oldest = ev.queuedAt
		}
	}
	if len(parts) == 0 {
		return "", types, oldest
	}
	merged = mergedSensingHeader + strings.Join(parts, "\n\n")
	merged = strings.ReplaceAll(merged, "\n\n\n", "\n\n")
	merged = strings.TrimSpace(merged)
	return merged, types, oldest
}
