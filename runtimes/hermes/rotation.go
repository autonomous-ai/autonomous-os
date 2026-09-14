package hermes

import (
	"fmt"
	"log/slog"
	"time"
)

// rotateMaxTurns / rotateTokenThreshold gate conversation rotation (see
// ShouldRotateSession). The generic handler's autoSessionThreshold (150k tokens)
// is useless for Hermes: the gateway compresses history server-side before each
// call, so os-server only ever observes ~20-60k tokens regardless of the real
// chain size — which grows to millions of tokens / tens of MB per device-main
// response blob and makes every turn reconstruct + recompress it (~1min/turn).
//
// The token net was 50_000 until 2026-09-09, when it turned out to sit INSIDE
// the normal operating range instead of above it. Observed on lamp-a0ae: a
// fresh conversation already reports ~12.3k (system prompt + SOUL + USER.md +
// skills), and an ordinary turn that reads a SKILL.md and runs a tool adds
// ~25k — 12.3k → 41.3k → 64.5k → 73.5k. The net fired every 2-3 turns, and
// because the wired path is maybeAutoNewSession (compact is disabled) each
// firing DROPPED the history with no summary: the device answered "that isn't
// in our current chat" about a draft it had written two turns earlier.
//
// 250_000 keeps roughly 10 turns at the observed +25k/turn while still catching
// a runaway chain. Same reasoning and same value as codex's safety net (see
// runtimes/codex/rotation.go): a net has to sit ABOVE where the backend's own
// compression settles, not inside it.
const (
	rotateMaxTurns       = 40
	rotateTokenThreshold = 250_000
)

// initConversation seeds the active conversation name once per process with a
// boot-unique suffix, so a restart never re-attaches to a previously bloated
// chain (the gateway keys its response history on the conversation name).
func (s *HermesService) initConversation() {
	s.convOnce.Do(func() {
		s.bootStamp = time.Now().Unix()
		s.conversation.Store(fmt.Sprintf("%s-%d", Conversation, s.bootStamp))
	})
}

// conversationName returns the active conversation name sent on every turn.
func (s *HermesService) conversationName() string {
	s.initConversation()
	name, _ := s.conversation.Load().(string)
	return name
}

// rotateConversation switches future turns to a fresh conversation name so the
// gateway starts a new (small) history chain. The old chain is abandoned (it
// remains on the gateway under the old name until a separate prune reclaims the
// disk). Clears lastResponseID so os-server stops correlating the old chain.
func (s *HermesService) rotateConversation() {
	s.initConversation()
	seq := s.rotateSeq.Add(1)
	name := fmt.Sprintf("%s-%d-%d", Conversation, s.bootStamp, seq)
	s.conversation.Store(name)
	s.lastResponseID.Store("")
	slog.Info("hermes conversation rotated", "component", "hermes", "conversation", name)
}

// ShouldRotateSession overrides the generic handler's token-threshold rotation
// decision (the sessionRotator optional interface). Hermes rotates on turn count
// (primary — the gateway blob grows ~one history snapshot per turn) or a token
// spike (secondary), because the generic 150k-token trigger never fires: the
// gateway compresses history before each call so os-server only observes
// ~20-60k tokens, never the real multi-million-token size.
func (s *HermesService) ShouldRotateSession(totalTokens, turnsSinceRotation int) bool {
	return turnsSinceRotation >= rotateMaxTurns || totalTokens >= rotateTokenThreshold
}

// NewSession rotates the conversation. Both the generic handler's auto-new-session
// path and an explicit factory reset land here. Instant — no gateway RPC.
func (s *HermesService) NewSession(sessionKey string) error {
	slog.Info("hermes NewSession: rotating conversation", "component", "hermes", "key", sessionKey)
	s.rotateConversation()
	return nil
}
