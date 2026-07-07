package codex

import (
	"log/slog"

	"go.autonomous.ai/os/domain"
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
const codexFallbackTokenThreshold = 150_000

// ShouldRotateSession rotates on real reported token count (see
// domain.AgentGateway). turn.completed usage maps input+cached+output into
// TotalTokens (translator.go), which approximates the live context size.
func (s *CodexService) ShouldRotateSession(totalTokens, _ int) bool {
	return totalTokens > codexFallbackTokenThreshold
}

// NewSession tells the bridge to drop the persisted thread id (session.new
// frame) so the next `codex exec` starts a fresh thread, and clears the local
// session key. Best-effort when the socket is down: the local clear still
// happens and the bridge's stale thread id will fail resume → the bridge
// retries fresh on its own.
func (s *CodexService) NewSession(sessionKey string) error {
	slog.Info("NewSession: requesting fresh codex thread", "component", "codex", "key", sessionKey)
	s.sessionUUID.Store("")
	if err := s.sendFrame(map[string]any{"type": "session.new"}); err != nil {
		slog.Warn("session.new frame send failed (bridge will retry fresh on resume failure)",
			"component", "codex", "error", err)
	}
	return nil
}
