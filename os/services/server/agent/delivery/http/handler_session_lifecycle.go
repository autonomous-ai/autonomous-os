package http

import (
	"errors"
	"log/slog"
	"time"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/lib/flow"
	"go.autonomous.ai/os/lib/hal"
	"go.autonomous.ai/os/lib/i18n"
)

// autoCompactCooldown is the minimum time between two compact triggers.
// Compact itself can run for 30-60s+ on the agent runtime; this guard
// prevents back-to-back fires while one is still in flight.
const autoCompactCooldown = 2 * time.Minute

// autoNewSessionCooldown is the minimum time between two new-session
// triggers. sessions.new is instant server-side but a token-usage burst
// across consecutive lifecycle.end events could otherwise drop the
// session more than once.
const autoNewSessionCooldown = 30 * time.Second

// maybeAutoCompact triggers a sessions.compact RPC when the backend's rotation
// policy fires (ShouldRotateSession).
//
// Currently disabled in favour of maybeAutoNewSession — kept here as
// reference / fallback. Re-enable by uncommenting the call site in
// handler_events.go if new-session causes memory regressions.
//
// Trade-off vs new-session:
//   - keeps verbatim conversation history via a generated summary
//   - blocks the agent for 30-60s+ while the summarize LLM call runs
//   - summary can override SKILL.md (see docs/agent-compaction.md)
func (h *AgentHandler) maybeAutoCompact(sessionKey string, totalTokens int, flowRunID string) {
	// Same per-backend rotation decision as maybeAutoNewSession (the two are
	// mutually exclusive — only one is wired at the call site, so the shared
	// turnsSinceRotation counter is incremented by exactly one of them).
	turns := int(h.turnsSinceRotation.Add(1))
	if !h.agentGateway.ShouldRotateSession(totalTokens, turns) {
		return
	}
	if !h.compacting.CompareAndSwap(false, true) {
		return
	}
	h.turnsSinceRotation.Store(0)
	slog.Info("auto-compact triggered", "component", "agent",
		"total_tokens", totalTokens, "turns", turns)
	flow.Log("compact_triggered", map[string]any{
		"session": sessionKey,
		"tokens":  totalTokens,
	}, flowRunID)
	go func() {
		defer time.AfterFunc(autoCompactCooldown, func() {
			h.compacting.Store(false)
		})
		if err := hal.SpeakInterruptible(i18n.One(i18n.PhraseCompactNotice)); err != nil {
			slog.Warn("compaction notice TTS failed", "component", "agent", "backend", h.agentGateway.Name(), "error", err)
		}
		if sessionKey == "" {
			slog.Error("auto-compact failed: no session key", "component", "agent")
			return
		}
		if err := h.agentGateway.CompactSession(sessionKey); err != nil {
			if errors.Is(err, domain.ErrNotSupportedByRuntime) {
				slog.Info("auto-compact skipped: backend has no compact API",
					"component", "agent", "backend", h.agentGateway.Name())
			} else {
				slog.Error("auto-compact failed", "component", "agent", "error", err)
			}
		}
	}()
}

// maybeAutoNewSession triggers a sessions.new RPC when the backend's rotation
// policy fires (ShouldRotateSession). Replaces compact for the latency-sensitive
// case: sessions.new completes instantly on the agent runtime so the user does
// not see the 30-60s freeze that compact causes.
//
// Trade-off vs compact:
//   - loses verbatim in-session conversation flow ("what we said an
//     hour ago")
//   - keeps all device external memory: mood log, habit tracking, voice
//     clusters, owner identity, music suggestion history — those live
//     outside the agent session JSONL and survive a session swap
//   - no TTS notice — the swap is meant to be invisible
func (h *AgentHandler) maybeAutoNewSession(sessionKey string, totalTokens int, flowRunID string) {
	turns := int(h.turnsSinceRotation.Add(1))
	// Rotation policy is per-backend (ShouldRotateSession): OpenClaw/PicoClaw use
	// a real-token threshold, Hermes uses turn count (its reported tokens are
	// post-compression and never reflect the real chain size). See
	// domain.AgentGateway.
	if !h.agentGateway.ShouldRotateSession(totalTokens, turns) {
		return
	}
	if !h.newSessioning.CompareAndSwap(false, true) {
		return
	}
	h.turnsSinceRotation.Store(0)
	slog.Info("auto-new-session triggered", "component", "agent",
		"total_tokens", totalTokens, "turns", turns)
	flow.Log("new_session_triggered", map[string]any{
		"session": sessionKey,
		"tokens":  totalTokens,
		"turns":   turns,
	}, flowRunID)
	go func() {
		defer time.AfterFunc(autoNewSessionCooldown, func() {
			h.newSessioning.Store(false)
		})
		if sessionKey == "" {
			slog.Error("auto-new-session failed: no session key", "component", "agent")
			return
		}
		if err := h.agentGateway.NewSession(sessionKey); err != nil {
			slog.Error("auto-new-session failed", "component", "agent", "error", err)
		}
	}()
}
