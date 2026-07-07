package claudecode

import (
	"log/slog"

	"go.autonomous.ai/os/domain"
)

// CompactSession — Claude Code auto-compacts its own context when it
// approaches the window limit; there is no external compact RPC to call.
// Returns ErrNotSupportedByRuntime so callers see nothing was done here.
func (s *ClaudeCodeService) CompactSession(sessionKey string) error {
	slog.Info("CompactSession: not supported (claudecode auto-compacts)", "component", "claudecode", "session", sessionKey)
	return domain.ErrNotSupportedByRuntime
}

// ShouldRotateSession — never rotate: Claude Code manages its own context window
// (auto-compaction), so an os-server-driven rotation would only throw context
// away. NewSession stays available for an explicit user reset.
func (s *ClaudeCodeService) ShouldRotateSession(_, _ int) bool {
	return false
}

// NewSession asks the bridge to restart Claude Code WITHOUT --resume, starting a
// fresh session; the local session id is dropped so the init event of the new
// session is adopted.
func (s *ClaudeCodeService) NewSession(sessionKey string) error {
	slog.Info("NewSession: requesting fresh claude session", "component", "claudecode", "key", sessionKey)
	s.sessionUUID.Store("")
	if err := s.sendFrame(map[string]any{"type": "session.new"}); err != nil {
		// Not connected — the bridge never saw the request, so its session.json
		// still resumes the old session on the next spawn. Best-effort by design:
		// rotation is user-triggered and can simply be retried once the bridge
		// is back.
		slog.Warn("NewSession: bridge not reachable", "component", "claudecode", "error", err)
	}
	return nil
}
