package codex

import (
	"log/slog"

	"go.autonomous.ai/os/system/domain"
)

// CompactSession — Codex does not expose a compact API; rotate via
// NewSession instead.
func (s *CodexService) CompactSession(sessionKey string) error {
	slog.Info("CompactSession: not supported (codex backend)", "component", "codex", "session", sessionKey)
	return domain.ErrNotSupportedByRuntime
}

// codexFallbackTokenThreshold is a safety net only: Codex auto-compacts its
// own context (model_auto_compact_token_limit), so the reported per-turn
// input stays bounded and this rarely fires. It exists so a runaway thread
// (compaction bug, oversized tool outputs) still gets rotated.
//
// Was 150_000, which made the net fire on ordinary turns instead of runaway
// ones — device-observed 2026-08-24 on lamp-0c89: 3 of 8 consecutive sensing
// turns crossed it (context 153k / 170k), each rotation dropped the thread, and
// the fresh thread then re-read every SKILL.md by shell (6 calls, ~60s) which
// pushed the context straight back over the line. A net has to sit ABOVE where
// codex's own compaction settles, not inside it; the largest healthy turn seen
// was 170_872. 250_000 is a judgement call on that evidence — revisit if a
// runaway thread ever gets past it.
const codexFallbackTokenThreshold = 250_000

// ShouldRotateSession rotates on the live CONTEXT size — input + cached as
// reported by the last turn.completed (s.lastContextTokens, stashed in
// translator.go). The totalTokens the shared handler passes folds in this
// turn's output, which is turn volume rather than context, so it is used only
// as a fallback before the first usage frame of the process arrives.
func (s *CodexService) ShouldRotateSession(totalTokens, _ int) bool {
	contextTokens := int(s.lastContextTokens.Load())
	if contextTokens == 0 {
		contextTokens = totalTokens
	}
	return contextTokens > codexFallbackTokenThreshold
}

// NewSession tells the bridge to drop the persisted thread id (session.new
// frame) so the next `codex exec` starts a fresh thread, and clears the local
// session key. Best-effort when the socket is down: the local clear still
// happens and the bridge's stale thread id will fail resume → the bridge
// retries fresh on its own.
func (s *CodexService) NewSession(sessionKey string) error {
	slog.Info("NewSession: requesting fresh codex thread", "component", "codex", "key", sessionKey)
	s.sessionUUID.Store("")
	// The fresh thread starts empty — drop the old thread's context size so a
	// turn that completes without a usage frame cannot re-trip the net on it.
	s.lastContextTokens.Store(0)
	if err := s.sendFrame(map[string]any{"type": "session.new"}); err != nil {
		slog.Warn("session.new frame send failed (bridge will retry fresh on resume failure)",
			"component", "codex", "error", err)
	}
	return nil
}
