package hermes

import (
	"log/slog"
	"sync"
	"time"
)

// slackStreamFlushInterval throttles chat.appendStream calls. chat.appendStream is
// Tier 4 (100+/min); ~1.5/sec stays comfortably under that while keeping the text
// visibly streaming. The FIRST flush is immediate (kick), so content appears as soon
// as it is ready.
const slackStreamFlushInterval = 650 * time.Millisecond

// slackStream is a live Slack streaming reply for one run. chat.startStream is opened
// lazily on the FIRST content chunk (seeded with that text), so the message is never a
// visibly-empty bubble — the assistant status ("Generating response…") covers the
// pre-text think time. A dedicated goroutine appends the cleaned reply text in order so
// the agent-event delta loop never blocks on Slack HTTP calls. The handler feeds the
// cleaned cumulative text via StreamSlackDelta; the goroutine appends only the new tail.
type slackStream struct {
	channel  string
	threadTS string
	teamID   string

	mu          sync.Mutex
	ts          string // streaming message ts — set once chat.startStream succeeds
	started     bool   // true once chat.startStream has opened the message
	latest      string // latest cleaned cumulative reply text
	appendedLen int    // bytes of `latest` already sent to Slack

	kick    chan struct{} // nudges an immediate (throttled) flush on new content
	stop    chan struct{} // closed by finishSlackStream to end the goroutine
	stopped chan struct{} // closed by the goroutine after its final flush + stopStream
}

// startSlackStreamSession registers a (not-yet-opened) stream for runID and starts its
// append loop. chat.startStream is called lazily on the first content.
func (s *HermesService) startSlackStreamSession(runID, channel, threadTS, teamID string) {
	st := &slackStream{
		channel:  channel,
		threadTS: threadTS,
		teamID:   teamID,
		kick:     make(chan struct{}, 1),
		stop:     make(chan struct{}),
		stopped:  make(chan struct{}),
	}
	s.slackStreamsMu.Lock()
	s.slackStreams[runID] = st
	s.slackStreamsMu.Unlock()
	go s.runSlackStream(st)
}

// runSlackStream flushes new text to Slack on each kick (throttled) / tick until stopped.
func (s *HermesService) runSlackStream(st *slackStream) {
	ticker := time.NewTicker(slackStreamFlushInterval)
	defer ticker.Stop()
	var lastFlush time.Time
	for {
		select {
		case <-st.kick:
			s.flushSlackStream(st, &lastFlush, false)
		case <-ticker.C:
			s.flushSlackStream(st, &lastFlush, false)
		case <-st.stop:
			s.flushSlackStream(st, &lastFlush, true) // final flush, forced
			st.mu.Lock()
			started, ts := st.started, st.ts
			st.mu.Unlock()
			if started {
				if err := s.stopSlackStream(st.channel, ts); err != nil {
					slog.Debug("slack: stopStream failed (non-fatal)", "component", "hermes", "err", err)
				}
			}
			close(st.stopped)
			return
		}
	}
}

// flushSlackStream sends the un-sent tail of latest. It opens the stream
// (chat.startStream, seeded with the first tail) on the first call that has content,
// then appends subsequent tails (chat.appendStream). appendedLen only advances on a
// successful call, so a transient failure retries the same tail next flush. force
// bypasses the throttle (final flush).
func (s *HermesService) flushSlackStream(st *slackStream, lastFlush *time.Time, force bool) {
	if !force && time.Since(*lastFlush) < slackStreamFlushInterval {
		return
	}
	st.mu.Lock()
	pending := ""
	if len(st.latest) > st.appendedLen {
		pending = st.latest[st.appendedLen:]
	}
	started := st.started
	st.mu.Unlock()
	if pending == "" {
		return
	}

	if !started {
		ts, err := s.startSlackStream(st.channel, st.threadTS, st.teamID, pending)
		if err != nil {
			slog.Debug("slack: startStream failed (non-fatal, will fall back)", "component", "hermes", "err", err)
			return // not started; appendedLen unchanged → retry / fallback
		}
		st.mu.Lock()
		st.ts, st.started = ts, true
		st.appendedLen += len(pending)
		st.mu.Unlock()
	} else {
		if err := s.appendSlackStream(st.channel, st.ts, pending); err != nil {
			slog.Debug("slack: appendStream failed (non-fatal)", "component", "hermes", "err", err)
			return // appendedLen unchanged → retry next flush
		}
		st.mu.Lock()
		st.appendedLen += len(pending)
		st.mu.Unlock()
	}
	*lastFlush = time.Now()
}

// StreamSlackDelta implements domain.SlackBridge — records the latest cleaned
// cumulative text for runID and nudges the append loop. Monotonic guard: ignore a
// shorter snapshot (a mid-stream sanitize that briefly shrank the text).
func (s *HermesService) StreamSlackDelta(runID, cleanTextSoFar string) {
	s.slackStreamsMu.Lock()
	st := s.slackStreams[runID]
	s.slackStreamsMu.Unlock()
	if st == nil {
		return
	}
	st.mu.Lock()
	if len(cleanTextSoFar) >= len(st.latest) {
		st.latest = cleanTextSoFar
	}
	st.mu.Unlock()
	select {
	case st.kick <- struct{}{}:
	default:
	}
}

// finishSlackStream finalizes the stream for runID: records the final text, signals the
// goroutine to flush + chat.stopStream, waits, and removes the session. Returns true
// when the stream was actually opened (reply delivered via streaming); false when it
// never opened (the caller then posts a chat.postMessage fallback). No-op (false) when
// absent.
func (s *HermesService) finishSlackStream(runID, finalText string) bool {
	s.slackStreamsMu.Lock()
	st := s.slackStreams[runID]
	delete(s.slackStreams, runID)
	s.slackStreamsMu.Unlock()
	if st == nil {
		return false
	}
	if finalText != "" {
		st.mu.Lock()
		if len(finalText) >= len(st.latest) {
			st.latest = finalText
		}
		st.mu.Unlock()
	}
	close(st.stop)
	<-st.stopped
	st.mu.Lock()
	started := st.started
	st.mu.Unlock()
	return started
}
