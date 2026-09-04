package codex

import (
	"log/slog"
	"os"
	"sort"
	"strconv"
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

// busyTTL bounds how long the busy flag survives without a terminal frame. It
// exists for ONE case: the turn's final frame was DROPPED, so the sensing
// pipeline would otherwise wedge forever. It must therefore stay LONGER than
// the gatewayd's per-turn timeout — the gatewayd always ends a turn (completed,
// failed, or its own "timeout"), so any TTL shorter than that fires on turns
// that are merely SLOW.
//
// A chat turn is EXPECTED to be slow: a user opens chat precisely for work that
// takes a while ("build me a Three.js scene"), and the same prompt answered over
// Telegram/OpenClaw runs 35 minutes to completion. The old fixed 5 minutes was
// half the 10-minute turn timeout, so every such turn tripped it — and this path
// additionally called clearTurn(), wiping the IN-FLIGHT run id. That orphaned the
// browser's pending run: every later lifecycle/error frame found no current run,
// allocated a fresh id, and attached to an unrelated queued turn, so the chat sat
// on a pending bubble until its own deadline and reported "no response".
// Measured 2026-09-03 on lamp-0c89: run device-chat-139 started 15:40:41, lost
// its id at 15:45:41, and the 15:50:41 "timeout" landed on device-chat-168.
//
// Derived from CODEX_TURN_TIMEOUT_S (the same value the gatewayd enforces) plus a
// margin, so raising the turn timeout cannot silently re-open this bug.
const busyTTLMargin = 5 * time.Minute

func busyTTL() time.Duration {
	// Same default as the gatewayd's own Config (10 minutes). Long enough for a
	// heavy turn, short enough that a WEDGED one — the upstream stream going
	// silent mid-turn, measured on lamp-0c89 2026-09-03 — is killed and the
	// device recovers instead of being held for most of an hour.
	timeout := 10 * time.Minute
	if f, err := strconv.ParseFloat(strings.TrimSpace(os.Getenv("CODEX_TURN_TIMEOUT_S")), 64); err == nil && f > 0 {
		timeout = time.Duration(f * float64(time.Second))
	}
	return timeout + busyTTLMargin
}

// IsBusy mirrors openclaw's OpenclawService.IsBusy: true while a turn is in flight OR a
// chat.send is still waiting for its first inbound frame. Auto-clears after
// busyTTL if the final frame got dropped so the sensing pipeline cannot wedge.
func (s *CodexService) IsBusy() bool {
	if s.activeTurn.Load() {
		since := s.busySince.Load()
		if since > 0 && time.Since(time.UnixMilli(since)) > busyTTL() {
			slog.Warn("busy flag expired — auto-clearing (final frame likely missed)",
				"component", "codex", "stuck_for_s", int(time.Since(time.UnixMilli(since)).Seconds()))
			s.activeTurn.Store(false)
			go s.drainPendingEvents()
			return s.HasFreshPendingChatSend()
		}
		return true
	}
	return s.HasFreshPendingChatSend()
}

// SetBusy flips active state. Drains pending events on idle.
func (s *CodexService) SetBusy(busy bool) {
	if busy {
		s.busySince.Store(time.Now().UnixMilli())
	}
	s.activeTurn.Store(busy)
	if !busy {
		s.drainPendingEvents()
	}
}

func (s *CodexService) QueuePendingEvent(eventType, msg, image, fixedRunID string) {
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
// openclaw / hermes drain: voice events prioritised, expirable high-frequency
// types (presence / motion / emotion) coalesced to latest-only and stale entries
// dropped after expireAfter.
// DrainPendingEvents satisfies domain.AgentGateway. The idle edge is not the
// only reason a queued event waits — one queued because the SPEAKER was busy
// has no turn ending behind it to drain the queue.
func (s *CodexService) DrainPendingEvents() {
	s.drainPendingEvents()
}

func (s *CodexService) drainPendingEvents() {
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
