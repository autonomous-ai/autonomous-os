package hermes

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
	currentUser string
	fixedRunID  string
}

const busyTTL = 5 * time.Minute

// IsBusy mirrors openclaw.Service.IsBusy: true while a turn is in flight OR a
// chat.send is still waiting for response.created. Auto-clears after busyTTL
// if response.completed got dropped so the sensing pipeline cannot wedge.
func (s *Service) IsBusy() bool {
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
func (s *Service) SetBusy(busy bool) {
	if busy {
		s.busySince.Store(time.Now().UnixMilli())
	}
	s.activeTurn.Store(busy)
	if !busy {
		s.drainPendingEvents()
	}
}

func (s *Service) QueuePendingEvent(eventType, msg, image, fixedRunID string) {
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
func (s *Service) drainPendingEvents() {
	s.pendingEventsMu.Lock()
	events := s.pendingEvents
	s.pendingEvents = nil
	s.pendingEventsMu.Unlock()

	if len(events) == 0 {
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
	for _, ev := range events {
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
		} else {
			flow.End("sensing_input", turnStart, map[string]any{"path": "agent", "run_id": runID}, runID)
			flow.Log("agent_call", map[string]any{"type": ev.eventType, "run_id": runID}, runID)
			slog.Info("pending event replayed", "component", "sensing", "type", ev.eventType, "runId", runID)
		}
	}
}
